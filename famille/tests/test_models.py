import types

from django.contrib.auth.models import User, AnonymousUser
from django.core.exceptions import ObjectDoesNotExist
from django.db.models.signals import pre_save
from django.test import TestCase
from mock import MagicMock, patch

from famille import models
from famille.models.users import UserInfo, FamilleFavorite, PrestataireFavorite, Geolocation


__all__ = ["ModelsTestCase", "RatingTestCase"]


class ModelsTestCase(TestCase):

    def setUp(self):
        self.user1 = User.objects.create_user("a", "a@gmail.com", "a")
        self.famille = models.Famille(user=self.user1, email="a@gmail.com")
        self.famille.save()
        self.user2 = User.objects.create_user("b", "b@gmail.com", "b")
        self.presta = models.Prestataire(user=self.user2, description="Une description", email="b@gmail.com")
        self.presta.save()
        self.user3 = User.objects.create_user("c", "c@gmail.com", "c")
        self.famille_fav = FamilleFavorite(
            object_type="Prestataire", object_id=self.presta.pk, famille=self.famille
        )
        self.famille_fav.save()
        self.prestataire_fav = PrestataireFavorite(
            object_type="Famille", object_id=self.famille.pk, prestataire=self.presta
        )
        self.prestataire_fav.save()

    def tearDown(self):
        User.objects.all().delete()
        models.Famille.objects.all().delete()
        models.Prestataire.objects.all().delete()
        Geolocation.objects.all().delete()
        FamilleFavorite.objects.all().delete()
        PrestataireFavorite.objects.all().delete()

    def mock_process(self, target, args, kwargs, *_, **__):
        """
        Mocking multiprocessing.Process in order to test.
        """
        def start():
            target(*args, **kwargs)
        return MagicMock(start=start)

    def test_get_user_related(self):
        self.assertIsInstance(models.get_user_related(self.user1), models.Famille)
        self.assertIsInstance(models.get_user_related(self.user2), models.Prestataire)
        self.assertRaises(ObjectDoesNotExist, models.get_user_related, self.user3)

    def test_has_user_related(self):
        self.assertTrue(models.has_user_related(self.user1))
        self.assertTrue(models.has_user_related(self.user2))
        self.assertFalse(models.has_user_related(self.user3))

        anonymous = AnonymousUser()
        self.assertFalse(models.has_user_related(anonymous))

    def test_is_geolocated(self):
        geoloc = Geolocation(lat=33.01, lon=2.89)
        geoloc.save()

        self.assertFalse(self.famille.is_geolocated)

        self.famille.geolocation = geoloc
        self.famille.save()
        self.assertTrue(self.famille.is_geolocated)

    @patch("famille.utils.geolocation.geolocate")
    def test_geolocate(self, geolocate):
        geolocate.return_value = 48.895603, 2.322858
        self.famille.street = "32 rue des Epinettes"
        self.famille.postal_code = "75017"
        self.famille.city = "Paris"
        self.famille.country = "France"
        self.famille.save()

        self.famille.geolocate()
        geolocate.assert_called_with("32 rue des Epinettes 75017 Paris, France")
        self.assertIsNotNone(Geolocation.objects.filter(lat=48.895603, lon=2.322858).first())
        self.assertEqual(self.famille.geolocation.lat, 48.895603)
        self.assertEqual(self.famille.geolocation.lon, 2.322858)

    @patch("multiprocessing.Process")
    @patch("famille.models.users.UserInfo.geolocate")
    def test_signal(self, mock, process):
        process.side_effect = self.mock_process
        pre_save.connect(UserInfo._geolocate, sender=models.Famille, dispatch_uid="famille_geolocate")
        pre_save.connect(UserInfo._geolocate, sender=models.Prestataire, dispatch_uid="prestataire_geolocate")

        self.famille.country = "France"  # not enough
        self.famille.save()
        self.assertFalse(mock.called)

        self.famille.street = "32 rue des Epinettes"  # not enough
        self.famille.save()
        self.assertFalse(mock.called)

        self.famille.city = "Paris"  # enough info to geolocate
        self.famille.save()
        self.assertTrue(mock.called)
        mock.reset_mock()

        self.famille.geolocation = Geolocation(lat=1.2091, lon=2.289791)  # already geolocated
        self.famille.geolocation.save()
        self.famille.save()
        self.assertFalse(mock.called)

        pre_save.disconnect(sender=models.Famille, dispatch_uid="famille_geolocate")
        pre_save.disconnect(sender=models.Prestataire, dispatch_uid="prestataire_geolocate")

    def test_add_favorite(self):
        uri = "/api/v1/prestataires/%s" % self.presta.pk
        self.famille.add_favorite(uri)
        self.assertEqual(self.famille.favorites.all().count(), 1)
        qs = FamilleFavorite.objects.filter(
            famille=self.famille, object_id=self.presta.pk, object_type="Prestataire"
        )
        self.assertEqual(qs.count(), 1)

        # cannot add same favorite
        self.famille.add_favorite(uri)
        self.assertEqual(self.famille.favorites.all().count(), 1)
        qs = FamilleFavorite.objects.filter(
            famille=self.famille, object_id=self.presta.pk, object_type="Prestataire"
        )
        self.assertEqual(qs.count(), 1)

    def test_remove_favorite(self):
        uri = "/api/v1/prestataires/%s" % self.presta.pk
        FamilleFavorite(famille=self.famille, object_id=self.presta.pk, object_type="Prestataire").save()
        self.famille.remove_favorite(uri)

        self.assertEqual(self.famille.favorites.all().count(), 0)

    def test_get_favorites_data(self):
        favs = self.famille.get_favorites_data()
        self.assertIsInstance(favs, types.GeneratorType)
        favs = list(favs)
        self.assertEqual(len(favs), 1)
        self.assertIsInstance(favs[0], models.Prestataire)
        self.assertEqual(favs[0].description, self.presta.description)

    def test_get_resource_uri(self):
        out = "/api/v1/familles/%s" % self.famille.pk
        self.assertEqual(self.famille.get_resource_uri(), out)

        out = "/api/v1/prestataires/%s" % self.presta.pk
        self.assertEqual(self.presta.get_resource_uri(), out)

    def test_get_user(self):
        self.assertEqual(self.famille_fav.get_user(), self.presta)
        self.assertEqual(self.prestataire_fav.get_user(), self.famille)

    def test_create_user(self):
        user = models.UserInfo.create_user(self.user3, "prestataire")
        self.assertIsInstance(user, models.Prestataire)
        self.assertEqual(user.email, self.user3.email)

        user.delete()
        user = models.UserInfo.create_user(self.user3, "famille")
        self.assertIsInstance(user, models.Famille)
        self.assertEqual(user.email, self.user3.email)


class RatingTestCase(TestCase):

    def setUp(self):
        self.user1 = User.objects.create_user("a", "a@gmail.com", "a")
        self.famille = models.Famille(user=self.user1, email="a@gmail.com")
        self.famille.save()

    def tearDown(self):
        User.objects.all().delete()
        models.Famille.objects.all().delete()
        models.FamilleRatings.objects.all().delete()

    def test_average(self):
        rating = models.FamilleRatings(famille=self.famille)
        self.assertEqual(rating.average, 0)

        rating.reliability = 4
        self.assertEqual(rating.average, 1)

        rating.amability = 2
        rating.serious = 1
        rating.ponctuality = 3
        self.assertEqual(rating.average, 2.5)

    def test_user_nb_ratings(self):
        self.assertEqual(self.famille.nb_ratings, 0)
        models.FamilleRatings(famille=self.famille).save()
        models.FamilleRatings(famille=self.famille).save()
        self.assertEqual(self.famille.nb_ratings, 2)

    def test_user_rating(self):
        self.assertEqual(self.famille.total_rating, 0)
        models.FamilleRatings(
            famille=self.famille, reliability=4, amability=2,
            serious=1, ponctuality=3
        ).save()
        models.FamilleRatings(
            famille=self.famille, reliability=1, amability=3,
            serious=5, ponctuality=0
        ).save()
        self.assertEqual(self.famille.total_rating, 2.375)