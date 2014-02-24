var JS_ROOT = "famille/static/js/",
    BUILD_ROOT = JS_ROOT + "build/";

module.exports = function(grunt) {
    grunt.initConfig({
        // connect: {
        //     test_server: {
        //         options: {
        //             port: 8001,
        //             base: "famille/static/js",
        //             keepalive: grunt.option('keepalive')
        //         }
        //     }
        // },
        // mocha_phantomjs: {
        //     all: {
        //         options: {
        //             urls: ['http://localhost:8001/mocha.html']
        //         }
        //     }
        // },
        //** Bundleing JS tasks **
        browserify: {
            options: {
                debug: true
            },
            search: {
                dest: BUILD_ROOT + "search.js",
                src: [JS_ROOT + "search/app.js"]
            },
            // accounts: {
            //     dest: BUILD_ROOT + "libs.js",
            //     src: [JS_ROOT + "libs/base.js"]
            // },
        },
        watch: {
            options: {
                atBegin: true
            },
            all: {
                files: [JS_ROOT + '**/*.js'],
                tasks: ['build']
            }
        }
    });

    // Load tasks from "grunt-sample" grunt plugin installed via Npm.
    grunt.loadNpmTasks('grunt-browserify');
    grunt.loadNpmTasks('grunt-contrib-watch');
    // grunt.loadNpmTasks('grunt-contrib-connect');
    // grunt.loadNpmTasks('grunt-mocha-phantomjs');

    // App tasks
    grunt.registerTask('build', ['browserify:search']);
};
