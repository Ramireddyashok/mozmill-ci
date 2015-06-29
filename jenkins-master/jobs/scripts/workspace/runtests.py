#!/usr/bin/env python

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import glob
import optparse
import os
import subprocess
import sys
import uuid

here = os.path.dirname(os.path.abspath(__file__))


class Runner(object):

    def __init__(self, venv_path):
        self.venv_path = venv_path
        self.build_path = os.path.join(here, 'build')

        self.s3_bucket = None

        os.chdir(here)
        self.activate_venv()

    def activate_venv(self):
        os.chdir(os.path.join(here, 'firefox-ui-tests'))
        command = ['python', 'create_venv.py', '--with-optional-packages', self.venv_path]

        print('Calling command to create virtual environment: %s' % command)
        subprocess.check_call(command)

        os.chdir(here)

        dir = 'Scripts' if sys.platform == 'win32' else 'bin'
        env_activate_file = os.path.join(self.venv_path, dir, 'activate_this.py')

        # Activate the environment and set the VIRTUAL_ENV os variable
        execfile(env_activate_file, dict(__file__=env_activate_file))
        os.environ['VIRTUAL_ENV'] = self.venv_path

        command = ['pip', 'install', '-r', 'requirements.txt']

        print('Calling command to install additional Python packages: %s' % command)
        subprocess.check_call(command)

    def download_build(self, options, args):
        command = ['mozdownload',
                   '--destination=%s' % self.build_path,
                   '--type=%s' % options.build_type,
                   '--branch=%s' % options.branch,
                   '--build-id=%s' % options.build_id,
                   '--locale=%s' % options.build_locale,
                   '--platform=%s' % options.platform,
                   ]

        print('Calling command to download the installer: %s' % command)
        subprocess.check_call(command)

    def run_tests(self, options, args):
        import firefox_ui_tests
        import firefox_puppeteer
        import mozinstall
        import mozversion
        import s3
        from treeherder import FirefoxUITestJob, JobResultParser, TreeherderSubmission

        # Download the build under test
        self.download_build(options, args)

        installer_path = glob.glob(os.path.join(self.build_path, '*firefox-*'))[0]

        print('Installing the build: %s' % installer_path)
        install_path = mozinstall.install(installer_path, 'firefox')

        binary = mozinstall.get_binary(install_path, 'firefox')
        print('Binary installed to: %s' % binary)

        version_info = mozversion.get_version(binary=binary)
        repository = version_info['application_repository'].split('/')[-1]

        if options.type == 'update':
            changeset = options.update_target_revision[:12]
        else:
            changeset = version_info['application_changeset']

        job = None
        th = None

        if (os.environ.get('AWS_BUCKET')):
            self.s3_bucket = s3.S3Bucket(os.environ['AWS_BUCKET'])

        if os.environ.get('TREEHERDER_URL'):
            # Setup job for treeherder and post 'running' status
            job = FirefoxUITestJob(product_name=version_info['application_name'],
                                   locale=options.build_locale,
                                   group_name='Firefox UI Test - %s' % options.type,
                                   group_symbol='F%s' % options.type[0])

            if os.environ.get('BUILD_URL'):
                job.add_details(title='CI Build',
                                value=os.environ['BUILD_URL'],
                                content_type='link',
                                url=os.environ['BUILD_URL'])

            try:
                th = TreeherderSubmission(project=repository, revision=changeset,
                                          url=os.environ['TREEHERDER_URL'],
                                          key=os.environ['TREEHERDER_KEY_%s' % repository],
                                          secret=os.environ['TREEHERDER_SECRET_%s' % repository])
                th.submit_results(job)
            except Exception, e:
                print('Cannot post job information to treeherder: %s' % e.message)

        command = [
            'firefox-ui-update' if options.type == 'update' else 'firefox-ui-tests',
            '--binary=%s' % binary,
            '--log-xunit=report.xml',  # Enable XUnit reporting for Jenkins result analysis
            '--log-html=report.html',  # Enable HTML reports with screenshots
            '--log-tbpl=tbpl.log',
        ]

        if options.type == 'update':
            # Ensure to enable Gecko log output in the console because the file gets
            # overwritten with the second update run
            command.append('--gecko-log=-')

            if options.update_channel and options.update_channel != 'None':
                command.append('--update-channel=%s' % options.update_channel)
            if options.update_target_version and options.update_target_version != 'None':
                command.append('--update-target-version=%s' % options.update_target_version)
            if options.update_target_build_id and options.update_target_build_id != 'None':
                command.append('--update-target-buildid=%s' % options.update_target_build_id)

        elif options.type == 'functional':
            manifests = [firefox_puppeteer.manifest, firefox_ui_tests.manifest_functional]
            command.extend(manifests)

        elif options.type == 'remote':
            manifests = [firefox_ui_tests.manifest_remote]
            command.extend(manifests)

        print('Calling command to execute tests: %s' % command)
        failed = False

        try:
            subprocess.check_call(command)
        except:
            failed = True

            # Only for failing update tests add the HTTP.log as artifact
            http_log_url = self.upload_s3('http.log')
            if http_log_url:
                job.add_details(title='http.log',
                                value=http_log_url,
                                content_type='link',
                                url=http_log_url)
        finally:
            if job and th:
                try:
                    gecko_log_url = self.upload_s3('gecko.log')
                    if gecko_log_url:
                        job.add_details(title='gecko.log',
                                        value=gecko_log_url,
                                        content_type='link',
                                        url=gecko_log_url)

                    tbpl_log_url = self.upload_s3('tbpl.log')
                    if tbpl_log_url:
                        parser = JobResultParser('tbpl.log')
                        job.add_log_reference(os.path.basename('tbpl.log'), tbpl_log_url,
                                              parse_status='parsed')
                        job.add_artifact(name='text_log_summary',
                                         artifact_type='json',
                                         blob={'step_data': parser.failures_as_json(),
                                               'logurl': tbpl_log_url})

                    job.completed(result='testfailed' if failed else 'success')
                    th.submit_results(job)
                except Exception, e:
                    print('Cannot post job information to treeherder: %s' % e.message)

    def upload_s3(self, path):
        if not self.s3_bucket:
            return None

        try:
            remote_filename = '%s_%s' % (str(uuid.uuid4()), os.path.basename(path))
            return self.s3_bucket.upload(path, remote_filename)
        except Exception, e:
            print 'Failure uploading "%s" to S3: %s' % (path, str(e))
            return None


def main():
    parser = optparse.OptionParser()
    parser.add_option('--branch',
                      dest='branch',
                      help='The branch of the Github repository to use')
    parser.add_option('--type',
                      dest='type',
                      choices=['functional', 'remote', 'update'],
                      help='The type of tests to execute')
    parser.add_option('--platform',
                      dest='platform',
                      help='The platform identifier where the build gets executed')

    build_options = optparse.OptionGroup(parser, "Build specific options")
    build_options.add_option('--build-date',
                             dest='build_date',
                             help='The date when the build has been created.')
    build_options.add_option('--build-id',
                             dest='build_id',
                             help='The BUILDID of the build to test')
    build_options.add_option('--build-locale',
                             dest='build_locale',
                             default='en-US',
                             help='The locale of the build. Default: %default')
    build_options.add_option('--build-type',
                             dest='build_type',
                             choices=['daily', 'tinderbox', 'try'],
                             default='daily',
                             help='Type of the build (daily, tinderbox, try).'
                             ' Default: %default')
    build_options.add_option('--build-version',
                             dest='build_version',
                             help='The version of the build to test')
    parser.add_option_group(build_options)

    update_options = optparse.OptionGroup(parser, "Update test specific options")
    update_options.add_option('--update-channel',
                              dest='update_channel',
                              help='The update channel to use for the update test')
    update_options.add_option('--update-target-build-id',
                              dest='update_target_build_id',
                              help='The expected BUILDID of the updated build')
    update_options.add_option('--update-target-version',
                              dest='update_target_version',
                              help='The expected version of the updated build')
    update_options.add_option('--update-target-revision',
                              dest='update_target_revision',
                              help='The expected revision of the updated build')
    parser.add_option_group(update_options)

    (options, args) = parser.parse_args()

    if options.type == 'update' and options.update_target_revision is None:
        parser.error('--update-target-revision is a mandatory option')

    try:
        path = os.path.abspath(os.path.join(here, 'venv'))
        runner = Runner(venv_path=path)
        runner.run_tests(options, args)
    except subprocess.CalledProcessError as e:
        sys.exit(e)

if __name__ == '__main__':
    main()