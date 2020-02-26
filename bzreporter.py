#!/usr/bin/python3

from __future__ import print_function

import argparse
import bugzilla
import logging
import os
import re
import sys
from lxml import etree

#BUGZILLA_URL = 'bugzilla.redhat.com'
DEFAULT_BUGZILLA_URL = 'partner-bugzilla.redhat.com'
DEVEL_WHITEBOARD_MARK = 'gating_passed+'
RE_BZID = re.compile('@bz(\d+)')
RE_FILENAME = re.compile('@feature_file_name:(\S+)')


logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)

def get_parser():
    parser = argparse.ArgumentParser()

    parser.add_argument("--dry-run", action="store_true",
        help='Do not write any changes to bugzilla')
    parser.add_argument("--comment", action="store_true",
        help='Add comment with test result details to the bug')

    parser.add_argument("--location", required=True,
        help='Path to directory with junit *.xml files')

    parser.add_argument("--bugzilla-url", default=DEFAULT_BUGZILLA_URL,
        help='URL of the bugzilla instance')
    parser.add_argument('--product', required=True,
        help='Bugzilla product')
    parser.add_argument('--release', required=True,
        help='Internal target release (8.2.0,...)')
    parser.add_argument('--status', action='append',
        default=['NEW', 'ASSIGNED', 'POST', 'MODIFIED', 'ON_DEV', 'ON_QA'],
        help='Bug status')

    return parser


def find_bugzillas(txt):
    # XXX false positives in comments, scenario names... 
    return [int(bzid) for bzid in RE_BZID.findall(txt, re.MULTILINE)]


def process_file(junit_file_name):
    results = dict()
    with open(junit_file_name, 'r') as junit_file:
        tree = etree.parse(junit_file)
        for testcase_elem in tree.findall('//testcase'):
            testcase = dict(testcase_elem.attrib)
            testcase['system-out'] = testcase_elem.find('system-out').text
            if testcase['status'] == 'failed':
                failure = testcase_elem.find('failure')
                testcase['failure'] = dict(failure.attrib)
                testcase['failure']['details'] = failure.text
            featurefile = RE_FILENAME.search(testcase['system-out'], re.MULTILINE)
            if featurefile:
                featurefile = featurefile[1]
            testcase['featurefile'] = featurefile
            for bzid in find_bugzillas(testcase['system-out']):
                results.setdefault(bzid, []).append(testcase)
    return results


def parse_results(junit_dir):
    results = dict()
    for dirpath, dirnames, filenames in os.walk(junit_dir):
        for filename in filenames:
            if filename.endswith('.xml'):
                for bzid, cases in process_file(os.path.join(dirpath, filename)).items():
                    results.setdefault(bzid, []).extend(cases)
    return results


class BzReporter():

    def __init__(self, bugzilla_url, product, release, status, comment=False, dry_run=False):
        self.bugzilla_url = bugzilla_url
        self.product = product
        self.release = release
        self.status = status
        self.dry_run = dry_run
        self.comment = comment

        self.bzapi = bugzilla.RHBugzilla(self.bugzilla_url)
        if not self.bzapi.logged_in:
            logging.error('The application requires bugzilla credentials.')
            sys.exit(1)

    def get_bugs(self, bzids):
        '''
        Collect bugs from the bugzilla corresponding IDs referenced in tests
        '''
        # filter bugs of given product / internal_target_release / status
        query = self.bzapi.build_query(
            product=self.product,
            status=self.status,
            )
        query['cf_internal_target_release'] = self.release
        # leave out bugs already marked as passed in devel whiteboard
        query['f1'] = 'cf_devel_whiteboard'
        query['o1'] = 'notsubstring'
        query['v1'] = DEVEL_WHITEBOARD_MARK
        # get only those bugs that are referenced in the tests results
        query['bug_id'] = bzids
        return self.bzapi.query(query)

    def report_results(self, bug, results):
        '''
        Update the bug with given tests results
        '''
        logging.info('Processing "{}"...'.format(bug))
        if DEVEL_WHITEBOARD_MARK in bug.devel_whiteboard:
            logging.info('Results already saved to the bug, skipping.')
            return

        if not self.dry_run:
            update_dict = dict()
            # - check whether all tests have passed
            # - prepare comment with the tests results
            # - prepare whiteboard message with reference to feature file name
            comment = ["The gating tests results:"]
            all_passed = True
            whiteboard_message = []
            for result in results:
                test_name = "{}/{}".format(result['classname'], result['name'])
                if result['status'] == 'passed':
                    comment.append("="*25)
                    comment.append("Test: {}".format(test_name))
                    comment.extend(result['system-out'].split('\n'))
                    whiteboard_message.append(
                        'Test+:{}'.format(result['featurefile'] or result['classname']))
                elif result['status'] == 'skipped':
                    logging.info('Test "{}" has been skipped.'.format(test_name))
                else:
                    all_passed = False
                    logging.info('Test "{}" has failed.'.format(test_name))
            if not all_passed:
                logging.info('Some tests did not pass, skipping.')
                return
            if self.comment:
                update_dict['comment'] = '\n'.join(comment)

            # mark the bug as processed in devel_whiteboard field
            # and add feature filename to the devel_whiteboard
            if bug.devel_whiteboard:
                whiteboard_message.insert(0, bug.devel_whiteboard)
            whiteboard_message.append(DEVEL_WHITEBOARD_MARK)
            update_dict['devel_whiteboard'] = '\n'.join(whiteboard_message)

            self.bzapi.update_bugs([bug.id], self.bzapi.build_update(**update_dict))
            logging.info("The bug has been updated.")


def main():
    parser = get_parser()
    args = parser.parse_args()

    # gather test results grouped by the bug id
    test_results = parse_results(args.location)

    bzr = BzReporter(
        bugzilla_url=args.bugzilla_url,
        product=args.product,
        release=args.release,
        status=args.status,
        dry_run=args.dry_run)
    bugs = bzr.get_bugs(list(test_results.keys()))

    for bug in bugs:
        bzr.report_results(bug, test_results[bug.id])


if __name__ == '__main__':
    main()
