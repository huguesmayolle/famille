# -*- coding=utf-8 -*-
from collections import OrderedDict
from datetime import date, datetime
import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in
from django.core.exceptions import ObjectDoesNotExist
from django.core.urlresolvers import reverse
from django.db import models
from django.utils.timezone import utc
from paypal.standard.ipn.models import PayPalIPN
from paypal.standard.ipn.signals import payment_was_successful
from verification.models import Key, KeyGroup
from verification.signals import key_claimed

from famille import errors
from famille.models.base import BaseModel
from famille.utils import (
    parse_resource_uri, geolocation, IMAGE_TYPES, DOCUMENT_TYPES,
    fields as extra_fields, payment
)
from famille.utils.mail import (
    send_mail_from_template_with_noreply,
    encode_recipient, decode_recipient_list
)
from famille.utils.python import pick, get_age_from_date


__all__ = [
    "Famille", "Prestataire", "Enfant", "Criteria",
    "get_user_related", "Reference", "UserInfo",
    "has_user_related", "user_is_located", "Geolocation",
    "compute_user_visibility_filters", "get_user_pseudo",
    "check_plan_expiration"
]


class Geolocation(BaseModel):
    """
    A model that represent a geolocation
    of a user.
    """
    lat = models.FloatField(null=True)
    lon = models.FloatField(null=True)
    has_error = models.BooleanField(default=False)

    class Meta:
        app_label = 'famille'

    @classmethod
    def from_postal_code(cls, postal_code):
        """
        Geolocation user from postal code. This is useful
        for search using postal code.
        """
        lat, lon = geolocation.geolocate("%s France" % postal_code)  # FIXME: this will obviously only work in France
        return cls(lat=lat, lon=lon)

    def geolocate(self, address):
        """
        Geolocate a geolocation object, given an address.

        :param address:         the address to geolocate
        """
        try:
            lat, lon = geolocation.geolocate(address)
        except errors.GeolocationError:
            self.has_error = True
            self.lat = None
            self.lon = None
        else:
            self.has_error = False
            self.lat = lat
            self.lon = lon

        self.save()


def get_user_related(user):
    """
    Return the user related model. Either
    Famille or Prestataire.

    :param user:      Django user
    """
    try:
        return user.famille
    except Famille.DoesNotExist:
        return user.prestataire


def has_user_related(user):
    """
    Find out if a user is related to a UserInfo subclass.

    :param user:      Django user
    """
    try:
        get_user_related(user)
        return True
    except (ObjectDoesNotExist, AttributeError):
        return False


def user_is_located(user):
    """
    Find out if a user is geolocated or not.

    :param user:       Django user
    """
    if not has_user_related(user):
        return False

    related = get_user_related(user)
    return related.is_geolocated


def compute_user_visibility_filters(user):
    """
    Compute the filters that will filter the whole
    user queryset and remove the users that are not
    visible to the user passed as parameter (request.user).

    :param user:          a Django user (might be anonymous)
    """
    filters = models.Q(visibility_global=True)

    if has_user_related(user):
        user = get_user_related(user)
        if isinstance(user, Famille):
            filters &= models.Q(visibility_family=True)
        else:
            filters &= models.Q(visibility_prestataire=True)

    return filters


def get_user_pseudo(user):
    """
    Retrieve the user pseudo, given an
    instance of Django User. Useful for
    django-postman.

    :param user:       the Django user instance
    """
    related = get_user_related(user)
    return related.get_pseudo()


class UserInfo(BaseModel):
    """
    The common user info that a Famille and
    a Prestataire need.
    """
    FREE_PLAN_LIMIT = datetime(2014, 7, 1, tzinfo=utc)
    FREE_PLAN_EXPIRATION = None
    PLANS = {
        "premium": "premium",
        "basic": "basic"
    }
    MANDATORY_FIELDS_FOR_VISIBILITY = [
        "name", "first_name", "street", "postal_code", "city", "profile_pic"
    ]
    MIN_VISIBILITY_SCORE = 0

    user = models.OneToOneField(User)
    geolocation = models.OneToOneField(Geolocation, blank=True, null=True)
    ipn = models.OneToOneField(PayPalIPN, blank=True, null=True)
    name = models.CharField(blank=True, max_length=50)
    first_name = models.CharField(blank=True, max_length=50)
    email = models.EmailField(max_length=100, unique=True)

    street = models.CharField(blank=True, null=True, max_length=100)
    postal_code = models.CharField(blank=True, null=True, max_length=8)
    city = models.CharField(blank=True, null=True, max_length=40)
    country = models.CharField(blank=True, max_length=20, default="France")
    tel = models.CharField(blank=True, null=True, max_length=15)
    tel_visible = models.BooleanField(blank=True, default=False)
    profile_pic = extra_fields.ContentTypeRestrictedFileField(
        upload_to=extra_fields.upload_to_timestamp("profile_pic"), blank=True, null=True,
        content_types=IMAGE_TYPES.values(), extensions=IMAGE_TYPES.keys(),
        max_upload_size=2621440  # 2.5MB
    )
    plan = models.CharField(blank=True, max_length=20, default="basic", choices=PLANS.items())
    plan_expires_at = models.DateTimeField(blank=True, null=True)
    newsletter = models.BooleanField(blank=True, default=True)
    # visibility
    visibility_family = models.BooleanField(default=True, blank=True)
    visibility_prestataire = models.BooleanField(default=True, blank=True)
    visibility_global = models.BooleanField(default=True, blank=True)
    is_test = models.BooleanField(default=False, blank=True)

    class Meta:
        abstract = True

    def __unicode__(self):
        return unicode(self.__str__())

    def __str__(self):
        return self.name

    @classmethod
    def create_user(cls, dj_user, type):
        """
        Create a user from data. It distinguishes between
        Famille and Prestataire types.

        :param dj_user:      the auth.User to link to
        :param type:         the type of user
        """
        UserType = Prestataire if type == "prestataire" else Famille
        user = UserType(user=dj_user, email=dj_user.email)
        if datetime.now(utc) <= UserType.FREE_PLAN_LIMIT:
            user.plan = cls.PLANS["premium"]
            user.plan_expires_at = UserType.FREE_PLAN_EXPIRATION

        user.save()
        return user

    @classmethod
    def decode_users(cls, data):
        """
        Decode a list of users. Uses mail.decode_recipient_list.

        :param data:        the encoded data
        """
        try:
            user_data = decode_recipient_list(data)
        except (TypeError, ValueError):
            raise ValueError("Wrong format")

        users = []
        for u in user_data:
            try:
                kls = Prestataire if u["type"] == "Prestataire" else Famille
                users.append(kls.objects.get(pk=u["pk"]))
            except (KeyError, kls.DoesNotExist):
                raise ValueError("Wrong format")
        return users

    @property
    def is_geolocated(self):
        """
        A property to check if a user is geolocated.
        """
        return self.geolocation and not self.geolocation.has_error

    @property
    def is_premium(self):
        """
        Return True if user is premium.
        """
        return self.plan == self.PLANS["premium"]

    @property
    def nb_ratings(self):
        """
        Return the number of total ratings the user received.
        """
        return self.ratings.all().count()

    @property
    def total_rating(self):
        """
        A property that computes the overall rating of a given user.
        """
        nb_ratings = self.nb_ratings
        if not nb_ratings:
            return 0
        return sum(rating.average for rating in self.ratings.all()) / float(nb_ratings)

    @property
    def total_rating_percent(self):
        """
        Return the total rating percent.
        """
        return int(self.total_rating / 5.0 * 100)

    @property
    def is_social(self):
        """
        Return True if user is connected through social auth.
        """
        return self.user.social_auth.all().count()

    @property
    def encoded(self):
        return encode_recipient(self)

    @property
    def visibility_score(self):
        """
        Compute the visibility score of the user.
        If less than a certain value, the user won't show up
        in the result.
        """
        score = 0
        for field in self.MANDATORY_FIELDS_FOR_VISIBILITY:
            if getattr(self, field):
                score += 1
        return float(score) / len(self.MANDATORY_FIELDS_FOR_VISIBILITY)

    @property
    def visibility_score_is_enough(self):
        """
        Return True if the visibility score is superior to
        self.MIN_VISIBILITY_SCORE.
        """
        return self.visibility_score >= self.MIN_VISIBILITY_SCORE

    # FIXME: can be async
    def geolocate(self):
        """
        Geolocate a user, using google geolocation.
        It basically calls the Google API and
        saves the GPS coordinates into database.

        NB: since geolocate is called in a pre_save signal,
            we do not save the model to save time, since
            it'll be saved soon enough.
        """
        address = "%s %s %s, %s" % (
            self.street or "",
            self.postal_code or "",
            self.city or "",
            self.country or "France"  # FIXME: this will obviously only work in France
        )

        # make sure the one to one field is set when having a pk
        g = Geolocation() if not self.geolocation else self.geolocation
        g.geolocate(address)

        self.geolocation = g
        self.save()

    def manage_geolocation(self, changed_data):
        """
        Manage the user geolocation. If street / postal code /
        city / country is in changed data, and if city / country
        at least is not None, the geolocation will be triggered.

        :param changed_data:        the data that changed on the model
        """
        condition = (
            any(field in changed_data for field in ("street", "postal_code", "city", "country"))
            and (self.city or self.postal_code)
        )
        if condition:
            self.geolocate()

    def add_favorite(self, resource_uri):
        """
        Favorite an object for the user.

        :param resource_uri:          the URI of the resource to favorite
        """
        object_type, object_id = parse_resource_uri(resource_uri)
        self.favorites.get_or_create(object_type=object_type.title(), object_id=int(object_id))

    def remove_favorite(self, resource_uri):
        """
        Unfavorite an object for the user.

        :param resource_uri:      the URI of the resource to unfavorite
        """
        object_type, object_id = parse_resource_uri(resource_uri)
        object_type = object_type.title()
        object_id = int(object_id)
        self.favorites.filter(object_type=object_type, object_id=object_id).delete()

    def get_favorites_data(self):
        """
        Retrieve the favorites data.
        """
        # FIXME: can become greedy in the future
        favs = OrderedDict([
            ("Prestataire", []),
            ("Famille", [])
        ])
        for favorite in self.favorites.all():
            fav = favorite.get_user()
            favs[fav.__class__.__name__].append(fav)

        return favs

    # FIXME: nothing to do here...
    def get_resource_uri(self):
        """
        Return the API uri of the objet. Don't really like it but for now
        didn't find another way.
        """
        return "/api/v1/%ss/%s" % (self.__class__.__name__.lower(), self.pk)

    def get_pseudo(self):
        """
        Return the pseudo of a user.
        Possible values:
            - FirstName
            - FirstName N. (first letter of name)
            - firstpartofemailaddress (before @)
        """
        pseudo = self.first_name
        if not pseudo:
            pseudo = self.email.split("@")[0]
        elif self.name:
            pseudo += " %s." % self.name[0]

        return pseudo

    def profile_access_is_authorized(self, request):
        """
        Athorize profile access only if request has the right to.

        :param request:            the request to be verified
        """
        if not has_user_related(request.user):
            return False

        if self.user == request.user:
            return True

        if not self.visibility_global:
            return False

        user = get_user_related(request.user)
        return self.visibility_prestataire if isinstance(user, Prestataire) else self.visibility_family

    def send_verification_email(self, request):
        """
        Send a verification email to a user after signup.
        """
        keygroup = KeyGroup.objects.get(name="activate_account")
        key = Key.generate(group=keygroup)
        key.claimed_by = self.user
        key.claimed = None
        key.save()
        activate_url = reverse('verification-claim-get', kwargs={'key': key, 'group': key.group})
        activate_url = request.build_absolute_uri(activate_url)
        send_mail_from_template_with_noreply(
            "email/verification.html", {"activate_url": activate_url},
            subject=u"Email de vérification", to=[self.email]
        )

    @classmethod
    def verify_user(*args, **kwargs):
        """
        Verify a user.
        """
        claimant = kwargs['claimant']
        if has_user_related(claimant):
            claimant.is_active = True
            claimant.save()
        else:
            logging.warning("Cannot verify user since claimant is unrelated.")


class Criteria(UserInfo):
    STUDIES = (
        ("brevet", u"Brevet"),
        ("bac", u"Bac"),
        ("+1", u"Bac +1"),
        ("+2", u"Bac +2"),
        ("+3", u"Bac +3"),
        ("+4", u"Bac +4"),
        ("+5", u"Bac +5"),
        ("other", u"Autre")
    )
    EXP_YEARS = (
        ("un", u"Moins d’un an"),
        ("trois", u"Entre 1 et 3 ans"),
        ("six", u"Entre 3 et 6 ans"),
        ("six+", u"Plus de 6 ans"),
    )

    type_garde = models.CommaSeparatedIntegerField(blank=True, null=True, max_length=50)
    studies = models.CharField(blank=True, null=True, max_length=10, choices=STUDIES)
    diploma = models.CommaSeparatedIntegerField(blank=True, null=True, max_length=50)
    experience_type = models.CommaSeparatedIntegerField(blank=True, null=True, max_length=50)
    experience_year = models.CharField(blank=True, null=True, max_length=10, choices=EXP_YEARS)
    menage = models.BooleanField(blank=True, default=False)
    repassage = models.BooleanField(blank=True, default=False)
    cuisine = models.BooleanField(blank=True, default=False)
    devoirs = models.BooleanField(blank=True, default=False)
    animaux = models.BooleanField(blank=True, default=False)
    non_fumeur = models.BooleanField(blank=True, default=False)
    psc1 = models.BooleanField(blank=True, default=False)
    permis = models.BooleanField(blank=True, default=False)
    enfant_malade = models.BooleanField(blank=True, default=False)
    tarif = models.CharField(blank=True, default="%s,%s" % settings.TARIF_RANGE, max_length=10)
    description = models.CharField(blank=True, null=True, max_length=400)
    language = models.CommaSeparatedIntegerField(blank=True, null=True, max_length=100)

    class Meta:
        abstract = True


class Prestataire(Criteria):
    """
    The Prestataire user.
    """
    FREE_PLAN_EXPIRATION = datetime(2015, 7, 1, tzinfo=utc)
    TYPES = (
        ("baby", "Baby-sitter"),
        ("mamy", "Mamie-sitter"),
        ("nounou", "Nounou"),
        ("maternel", "Assistant(e) maternel(le)"),
        ("parental", "Assistant(e) parental(e)"),
        ("pair", "Au pair"),
        ("other", "Autre"),
    )
    RESTRICTIONS = {
        "bebe": u"Bébé (0 à 1 an)",
        "jeune": u"Jeunes enfants (1 à 3 ans)",
        "marche": u"Enfants de 3 à 7 ans"
    }
    PAYMENT_PREFIX = "p"

    AGES = {
        "16-": u"Moins de 16 ans",
        "18-": u"Moins de 18 ans",
        "18+": u"Plus de 18 ans"
    }
    MANDATORY_FIELDS_FOR_VISIBILITY = UserInfo.MANDATORY_FIELDS_FOR_VISIBILITY + ["birthday", "type"]
    MIN_VISIBILITY_SCORE = float(settings.PRESTATAIRE_MIN_VISIBILITY_SCORE)

    birthday = models.DateField(null=True, blank=True)
    nationality = models.CharField(max_length=70, null=True, blank=True)
    type = models.CharField(max_length=40, choices=TYPES, null=True, blank=False)
    other_type = models.CharField(max_length=50, null=True, blank=True)  # FIXME: broken in the front?
    resume = extra_fields.ContentTypeRestrictedFileField(
        upload_to=extra_fields.upload_to_timestamp("resume"), blank=True, null=True,
        content_types=DOCUMENT_TYPES.values(), extensions=DOCUMENT_TYPES.keys(),
        max_upload_size=2621440  # 2.5MB
    )

    class Meta:
        app_label = 'famille'

    def get_age(self):
        return get_age_from_date(self.birthday)

    def get_type(self):
        """
        Return the prestataire type, depending on if he
        selected "Other".
        """
        return (self.get_type_display() if self.type != "other" else self.other_type) or ""


class Famille(Criteria):
    """
    The Famille user.
    """
    FREE_PLAN_EXPIRATION = datetime(2014, 9, 1, tzinfo=utc)
    TYPE_FAMILLE = {
        "mono": u"Famille monoparentale",
        "foyer": u"Famille Mère/Père au foyer",
        "actif": u"Les deux parents travaillent",
        "mi-actif": u"Un des deux parents travaille",
        "autre": u"Autre",
    }
    TYPE_ATTENTES_FAMILLE = (
        ("part", u"Garde partagée"),
        ("ecole", u"Sortie d'école"),
        ("urgences", u"Garde d'urgence (dépannages)"),
        ("nuit", u"Garde de nuit"),
        ("vacances", u"Vacances scolaires"),
        ("cond_sco", u"Conduite scolaire"),
        ("cond_peri", u"Conduite péri-scolaire"),
        ("dej", u"Echange de déjeuners"),
        ("other", u"Autre")
    )
    PAYMENT_PREFIX = "f"
    MIN_VISIBILITY_SCORE = float(settings.FAMILLE_MIN_VISIBILITY_SCORE)

    type = models.CharField(blank=True, null=True, max_length=10, choices=TYPE_FAMILLE.items())
    type_presta = models.CharField(blank=True, null=True, max_length=10, choices=Prestataire.TYPES)
    type_attente_famille = models.CharField(blank=True, null=True, max_length=15, choices=TYPE_ATTENTES_FAMILLE)

    class Meta:
        app_label = 'famille'


class Enfant(BaseModel):
    """
    An child of a Famille.
    """

    famille = models.ForeignKey(Famille, related_name="enfants")
    # compelled to do this naming because we cannot change the form field names...
    e_name = models.CharField(max_length=20, db_column="name")
    e_birthday = models.DateField(blank=True, null=True, db_column="birthday")
    e_school = models.CharField(blank=True, null=True, max_length=50, db_column="school")

    class Meta:
        app_label = 'famille'

    @property
    def display(self):
        """
        Display of the enfant.
        """
        disp = self.e_name
        if self.e_birthday:
            disp += u", %s ans" % get_age_from_date(self.e_birthday)
        if self.e_school:
            disp += u", scolarisé à %s" % self.e_school

        return disp


class BaseFavorite(BaseModel):
    """
    A model reprensenting a favorite.
    """
    OBJECT_TYPES = {
        'Prestataire': 'Prestataire',
        'Famille': 'Famille',
    }
    object_id = models.IntegerField()
    object_type = models.CharField(max_length=20, choices=OBJECT_TYPES.items())

    class Meta:
        abstract = True

    def __unicode__(self):
        return unicode(self.__str__())

    def __str__(self):
        return "%s %s" % (self.object_type, self.object_id)

    def get_user(self):
        """
        Return the user instance that is defined by the favorite.
        """
        return USER_CLASSES[self.object_type].objects.get(pk=self.object_id)


class FamilleFavorite(BaseFavorite):
    famille = models.ForeignKey(Famille, related_name="favorites")

    class Meta:
        app_label = 'famille'

    @property
    def owner(self):
        return self.famille


class PrestataireFavorite(BaseFavorite):
    prestataire = models.ForeignKey(Prestataire, related_name="favorites")

    class Meta:
        app_label = 'famille'

    @property
    def owner(self):
        return self.prestataire


class Reference(BaseModel):
    """
    A model representing a reference for a prestataire.
    """
    TYPE_GARDE = (
        ("Domicile des parents", "Domicile des parents"),
        ("Mon domicile", "Mon domicile"),
        ("Centre/colonie", "Centre/colonie"),
        ("Maison d’assistantes parentales", "Maison d’assistantes parentales"),
        ("Autre", "Autre")
    )

    prestataire = models.ForeignKey(Prestataire, related_name="references")
    name = models.CharField(max_length=100, null=True, blank=True)
    email = models.EmailField(null=True, blank=True)
    phone = models.CharField(blank=True, null=True, max_length=15)
    missions = models.TextField(blank=True, null=True)
    # FIXME: doesn't look like the proper field...
    referenced_user = models.OneToOneField(Famille, blank=True, null=True, related_name="reference_of")
    date_from = models.DateField(blank=True, null=True)
    date_to = models.DateField(blank=True, null=True)
    current = models.BooleanField(blank=True, default=False)
    garde = models.CharField(blank=True, null=True, max_length=40, choices=TYPE_GARDE)

    class Meta:
        app_label = 'famille'

    def get_famille_display(self):
        """
        Retrieve the famille, depending on reference type
        """
        if not self.referenced_user:
            return self.name
        return u"de %s (utilise notre site)" % self.referenced_user.get_pseudo()

    def get_dates_display(self):
        """
        Retrieve the dates of the reference for display.
        """
        if not self.date_from or not (self.current or self.date_to):
            return ""

        date_from = self.date_from.strftime("%d/%m/%Y")
        date_to = u"à aujourd'hui" if self.current else "au %s" % self.date_to.strftime("%d/%m/%Y")

        return u"Du %s %s" % (date_from, date_to)


# consts
USER_CLASSES = {
    "Famille": Famille,
    "Prestataire": Prestataire
}

FAVORITE_CLASSES = {
    Famille: FamilleFavorite,
    Prestataire: PrestataireFavorite
}


def check_plan_expiration(sender=None, request=None, user=None, related=None, **kwargs):
    """
    Verify user plan expiration upon user login.
    This will be connected to the user_logged_in signal.

    It will downgrade the user plan if:
        - the user plan has no expiration date
        - the user plan has expired
    And will send an email to the user.
    """
    if related or has_user_related(user):
        related = related or get_user_related(user)
        if related.is_premium:
            if related.plan_expires_at:
                expires = related.plan_expires_at
                now = datetime.now(utc) if expires.tzinfo else datetime.now()
                if expires >= now:
                    return

            related.plan = "basic"
            related.plan_expires_at = None
            related.save()
            send_mail_from_template_with_noreply(
                "email/plan.html", {},
                subject=u"Votre plan premium vient d'expirer", to=[related.email]
            )

# signals
payment_was_successful.connect(payment.signer.premium_signup, dispatch_uid="famille.premium")
key_claimed.connect(UserInfo.verify_user, dispatch_uid="famille.verify")
user_logged_in.connect(check_plan_expiration, dispatch_uid="famille.check_plan")
