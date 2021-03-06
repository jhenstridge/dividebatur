#!/usr/bin/env python3

import argparse
import tempfile
import logging
import difflib
import glob
import json
import sys
import os
import re
from pprint import pformat
from .counter import PapersForCount, SenateCounter
from .aecdata import CandidateList, SenateATL, SenateBTL, FormalPreferences
from .common import logger


class SenateCountPost2015:
    def __init__(self, state_name, get_input_file, **kwargs):
        self.candidates = CandidateList(state_name,
                                        get_input_file('all-candidates'),
                                        get_input_file('senate-candidates'))
        self.tickets_for_count = PapersForCount()

        self.s282_candidates = kwargs.get('s282_candidates')
        self.s282_method = kwargs.get('s282_method')
        self.max_tickets = kwargs['max_tickets'] if 'max_tickets' in kwargs else None

        self.remove_candidates = None
        remove = kwargs.get('remove_candidates')
        if remove:
            self.remove_candidates = [self.candidates.get_candidate_id(*t) for t in remove]

        def atl_flow(form):
            by_pref = {}
            for pref, group in zip(form, self.candidates.groups):
                if pref is None:
                    continue
                if pref not in by_pref:
                    by_pref[pref] = []
                by_pref[pref].append(group)
            prefs = []
            for i in range(1, len(form) + 1):
                at_pref = by_pref.get(i)
                if not at_pref or len(at_pref) != 1:
                    break
                the_pref = at_pref[0]
                for candidate in the_pref.candidates:
                    candidate_id = candidate.candidate_id
                    prefs.append(candidate_id)
            if not prefs:
                return None
            return prefs

        def btl_flow(form):
            by_pref = {}
            for pref, candidate in zip(form, self.candidates.candidates):
                if pref is None:
                    continue
                if pref not in by_pref:
                    by_pref[pref] = []
                by_pref[pref].append(candidate.candidate_id)
            prefs = []
            for i in range(1, len(form) + 1):
                at_pref = by_pref.get(i)
                if not at_pref or len(at_pref) != 1:
                    break
                candidate_id = at_pref[0]
                prefs.append(candidate_id)
            # must have unique prefs for 1..6, or informal
            if len(prefs) < 6:
                return None
            return prefs

        def resolve_non_s282(atl, btl):
            "resolve the formal form from ATL and BTL forms. BTL takes precedence, if formal"
            return btl_flow(btl) or atl_flow(atl)

        def resolve_s282_restrict_form(atl, btl):
            "resolve the formal form as for resolve_non_s282, but restrict to s282 candidates"
            expanded = btl_flow(btl) or atl_flow(atl)
            restricted = [candidate_id for candidate_id in expanded if candidate_id in self.s282_candidates]
            if len(restricted) == 0:
                return None
            return restricted

        def resolve_remove_candidates(atl, btl):
            "resolve the formal form, removing the listed candidates from eligibiity"
            restricted = None
            btl_expanded = btl_flow(btl)
            if btl_expanded:
                restricted = [candidate_id for candidate_id in btl_expanded if candidate_id not in self.remove_candidates]
                if len(restricted) < 6:
                    restricted = None
            if restricted is None:
                atl_expanded = atl_flow(atl)
                if atl_expanded:
                    restricted = [candidate_id for candidate_id in atl_expanded if candidate_id not in self.remove_candidates]
                    if len(restricted) == 0:
                        restricted = None
            return restricted

        def resolve_s282_restrict_form_with_savings(atl, btl):
            "resolve the formal form as for resolve_non_s282, but restrict to s282 candidates"
            restricted = None
            # if we were formal BTL in a non-s282 count, restrict the form. if at least one
            # preference, we're formal
            btl_expanded = btl_flow(btl)
            if btl_expanded:
                restricted = [candidate_id for candidate_id in btl_expanded if candidate_id in self.s282_candidates]
                if len(restricted) == 0:
                    restricted = None
            # if, before or after restriction, we are not formal BTL, try restricting the ATL form
            if restricted is None:
                atl_expanded = atl_flow(atl)
                if atl_expanded:
                    restricted = [candidate_id for candidate_id in atl_expanded if candidate_id in self.s282_candidates]
                    if len(restricted) == 0:
                        restricted = None
            return restricted

        atl_n = len(self.candidates.groups)
        btl_n = len(self.candidates.candidates)
        assert(atl_n > 0 and btl_n > 0)
        informal_n = 0
        n_ballots = 0
        resolution_fn = resolve_non_s282
        if self.s282_candidates:
            if self.s282_method == 'restrict_form':
                resolution_fn = resolve_s282_restrict_form
            elif self.s282_method == 'restrict_form_with_savings':
                resolution_fn = resolve_s282_restrict_form_with_savings
            else:
                raise Exception("unknown s282 method: `%s'" % (self.s282_method))
        if self.remove_candidates:
            resolution_fn = resolve_remove_candidates

        # the (extremely) busy loop reading preferences and expanding them into
        # forms to be entered into the count
        for raw_form, count in FormalPreferences(get_input_file('formal-preferences')):
            if self.max_tickets and n_ballots >= self.max_tickets:
                return
            atl = raw_form[:atl_n]
            btl = raw_form[atl_n:]
            form = resolution_fn(atl, btl)
            if form is not None:
                self.tickets_for_count.add_ticket(tuple(form), count)
            else:
                informal_n += count
            n_ballots += count
        # slightly paranoid check, but outside the busy loop
        assert(len(raw_form) == atl_n + btl_n)
        if informal_n > 0:
            logger.info("%d ballots are informal and were excluded from the count" % (informal_n))

    def get_tickets_for_count(self):
        return self.tickets_for_count

    def get_candidate_ids(self):
        candidate_ids = [c.candidate_id for c in self.candidates.candidates]
        if self.s282_candidates:
            candidate_ids = [t for t in candidate_ids if t in self.s282_candidates]
        if self.remove_candidates:
            candidate_ids = [t for t in candidate_ids if t not in self.remove_candidates]
        return candidate_ids

    def get_parties(self):
        return dict((c.party_abbreviation, c.party_name)
                    for c in self.candidates.candidates)

    def get_candidate_title(self, candidate_id):
        c = self.candidates.candidate_by_id[candidate_id]
        return "{}, {}".format(c.surname, c.given_name)

    def get_candidate_order(self, candidate_id):
        return self.candidates.candidate_by_id[candidate_id].candidate_order

    def get_candidate_party(self, candidate_id):
        return self.candidates.candidate_by_id[candidate_id].party_abbreviation


class SenateCountPre2015:
    def __init__(self, state_name, get_input_file, **kwargs):
        if 's282_recount' in kwargs:
            raise Exception('s282 recount not implemented for pre2015 data')

        self.candidates = CandidateList(state_name,
                                        get_input_file('all-candidates'),
                                        get_input_file('senate-candidates'))
        self.atl = SenateATL(
            state_name,
            get_input_file('group-voting-tickets'),
            get_input_file('first-preferences'))
        self.btl = SenateBTL(get_input_file('btl-preferences'))

        def load_tickets(ticket_obj):
            if ticket_obj is None:
                return
            for form, n in ticket_obj.get_tickets():
                self.tickets_for_count.add_ticket(form, n)
        self.tickets_for_count = PapersForCount()
        load_tickets(self.atl)
        load_tickets(self.btl)

    def get_tickets_for_count(self):
        return self.tickets_for_count

    def get_candidate_ids(self):
        return [c.candidate_id for c in self.candidates.candidates]

    def get_parties(self):
        return dict((c.party_abbreviation, c.party_name)
                    for c in self.candidates.candidates)

    def get_candidate_title(self, candidate_id):
        c = self.candidates.candidate_by_id[candidate_id]
        return "{}, {}".format(c.surname, c.given_name)

    def get_candidate_order(self, candidate_id):
        return self.candidates.candidate_by_id[candidate_id].candidate_order

    def get_candidate_party(self, candidate_id):
        return self.candidates.candidate_by_id[candidate_id].party_abbreviation


def verify_test_logs(verified_dir, test_log_dir):
    test_re = re.compile(r'^round_(\d+)\.json')
    rounds = []
    for fname in os.listdir(verified_dir):
        m = test_re.match(fname)
        if m:
            rounds.append(int(m.groups()[0]))

    def fname(d, r):
        return os.path.join(d, 'round_%d.json' % r)

    def getlog(d, r):
        with open(fname(d, r)) as fd:
            return json.load(fd)
    ok = True
    for idx in sorted(rounds):
        v = getlog(verified_dir, idx)
        t = getlog(test_log_dir, idx)
        if v != t:
            logger.error("Round %d: FAIL" % (idx))
            logger.error("Log should be:")
            logger.error(pformat(v))
            logger.error("Log is:")
            logger.error(pformat(t))
            logger.error("Diff:")
            logger.error(
                '\n'.join(
                    difflib.unified_diff(
                        pformat(v).split('\n'),
                        pformat(t).split('\n'))))
            ok = False
        else:
            logger.debug("Round %d: OK" % (idx))
    if ok and len(rounds) > 0:
        for fname in os.listdir(test_log_dir):
            if test_re.match(fname):
                os.unlink(os.path.join(test_log_dir, fname))
        os.rmdir(test_log_dir)
    return ok


def read_config(config_file):
    with open(config_file) as fd:
        return json.load(fd)


def cleanup_json(out_dir):
    for fname in glob.glob(out_dir + '/*.json'):
        logger.debug("cleanup: removing `%s'" % (fname))
        os.unlink(fname)


def write_angular_json(config, out_dir):
    json_f = os.path.join(out_dir, 'count.json')
    with open(json_f, 'w') as fd:
        obj = {
            'title': config['title']
        }
        obj['counts'] = [{
            'name': count['name'],
            'state': count['state'],
            'description': count['description'],
            'path': count['shortname']}
            for count in config['count']]
        json.dump(obj, fd, sort_keys=True, indent=4, separators=(',', ': '))


def get_data(input_cls, base_dir, count, **kwargs):
    aec_data = count['aec-data']

    def input_file(name):
        return os.path.join(base_dir, aec_data[name])
    return input_cls(count['state'], input_file, **kwargs)


def make_automation(answers):
    done = []

    def __auto_fn(*args):
        if len(done) == len(answers):
            return None
        # it makes sense for the JSON to be indexed from 1, so the JSON
        # data matches what's typed in on the console
        resp = answers[len(done)] - 1
        done.append(resp)
        return resp
    return __auto_fn


def json_count_path(out_dir, shortname):
    return os.path.join(out_dir, shortname + '.json')


def get_outcome(count, count_data, base_dir, out_dir, automation_fn=None):
    test_logs_okay = True
    test_log_dir = None
    if 'verified' in count:
        test_log_dir = tempfile.mkdtemp(prefix='dividebatur_tmp')
        logger.debug("test logs are written to: %s" % (test_log_dir))
    outf = json_count_path(out_dir, count['shortname'])
    logger.info("counting `%s'. output written to `%s'" % (count['name'], outf))
    counter = SenateCounter(
        outf,
        count['vacancies'],
        count_data.get_tickets_for_count(),
        count_data.get_parties(),
        count_data.get_candidate_ids(),
        count_data.get_candidate_order,
        count_data.get_candidate_title,
        count_data.get_candidate_party,
        test_log_dir,
        count.get('disable_bulk_exclusions'),
        name=count.get('name'),
        description=count.get('description'),
        house=count['house'],
        state=count['state'])
    if automation_fn is None:
        automation_fn = make_automation(count.get('automation', []))
    counter.set_election_order_callback(automation_fn)
    counter.set_candidate_tie_callback(automation_fn)
    counter.run()
    if test_log_dir is not None:
        if not verify_test_logs(os.path.join(base_dir, count['verified']), test_log_dir):
            test_logs_okay = False
    if not test_logs_okay:
        logger.error("** TESTS FAILED **")
        sys.exit(1)
    return (outf, counter.output.summary)


def get_input_method(format):
    # determine the counting method
    if format == 'AusSenatePre2015':
        return SenateCountPre2015
    elif format == 'AusSenatePost2015':
        return SenateCountPost2015


def check_counting_method_valid(method_cls, data_format):
    if method_cls is None:
        raise Exception("unsupported AEC data format '%s' requested" % (data_format))


def s282_options(out_dir, count, written):
    options = {}
    s282_config = count.get('s282')
    if not s282_config:
        return options
    options = {
        's282_method': s282_config['method']
    }
    shortname = s282_config['recount_from']
    if not shortname:
        return options
    fname = json_count_path(out_dir, shortname)
    if fname not in written:
        logger.error("error: `%s' needed for s282 recount has not been calculated during this dividebatur run." % (fname))
        sys.exit(1)
    with open(fname) as fd:
        data = json.load(fd)
    options['s282_candidates'] = [t['id'] for t in data['summary']['elected']]
    return options


def remove_candidates_options(count):
    options = {}
    remove_candidates = count.get('remove_candidates')
    if remove_candidates is None:
        return options
    options['remove_candidates'] = remove_candidates
    return options


def check_config(config):
    "basic checks that the configuration file is valid"
    shortnames = [count['shortname'] for count in config['count']]
    if len(shortnames) != len(set(shortnames)):
        logger.error("error: duplicate `shortname' in count configuration.")
        return False
    return True


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-q', '--quiet',
        action='store_true', help="Disable informational output")
    parser.add_argument(
        '-v', '--verbose',
        action='store_true', help="Enable debug output")
    parser.add_argument(
        'config_file',
        type=str,
        help='JSON config file for counts')
    parser.add_argument(
        'out_dir',
        type=str,
        help='Output directory')
    return parser.parse_args()


def execute_counts(out_dir, config_file):
    base_dir = os.path.dirname(os.path.abspath(config_file))
    config = read_config(config_file)
    if not check_config(config):
        return

    # global config for the angular frontend
    cleanup_json(out_dir)
    write_angular_json(config, out_dir)
    written = set()
    for count in config['count']:
        aec_data_config = count['aec-data']
        data_format = aec_data_config['format']
        input_cls = get_input_method(data_format)
        check_counting_method_valid(input_cls, data_format)
        count_options = {}
        count_options.update(s282_options(out_dir, count, written))
        count_options.update(remove_candidates_options(count))
        logger.debug("reading data for count: `%s'" % (count['name']))
        data = get_data(input_cls, base_dir, count, **count_options)
        logger.debug("determining outcome for count: `%s'" % (count['name']))
        outf, _ = get_outcome(count, data, base_dir, out_dir)
        written.add(outf)


def main():
    args = parse_args()
    if args.quiet:
        logger.setLevel(logging.ERROR)
    elif args.verbose:
        logger.setLevel(logging.DEBUG)
    execute_counts(args.out_dir, args.config_file)


if __name__ == '__main__':
    main()
