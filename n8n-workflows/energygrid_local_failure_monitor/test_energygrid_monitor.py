import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path

from energygrid_monitor_report import (CORE_SCHEMA, ReportError, _validate_endpoint, _json_file, bind_run_dir, main,
                                       summarise, _incident_ref, _closed_ref)

RUN='12345678-1234-1234-1234-123456789abc'


def core_reports():
    acq={'run_id':RUN,'acquisition':{'EB_BILL':{'status':'ALREADY_ADMITTED'},'TENANT_BILL':{'status':'EMPTY'}}}
    status={'streams':{'EB_BILL':{'state':'DRAINED','support_ref':None,'invoices':[]},
                       'TENANT_BILL':{'state':'DRAINED','support_ref':None,'invoices':[]}}}
    return acq,status

class EnergyGridMonitorReportTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,16,4,0,tzinfo=timezone.utc)

    def test_both_success_silent(self):
        a,s=core_reports();o=summarise(a,s,self.now)
        self.assertEqual('2026-10-16T04:00:00Z',o['observed_at_utc'])
        self.assertEqual('OK',o['streams']['EB_BILL']['result'])
        self.assertEqual('OK',o['streams']['TENANT_BILL']['result'])
        self.assertEqual({'schema','run_id','observed_at_utc','streams'},set(o))

    def test_failed_acquisition_precedes_drained_status(self):
        a,s=core_reports();a['acquisition']['TENANT_BILL']={'status':'LOGIN_FAILED','support_ref':'EG_TENANT_AUTH_FAILED'}
        o=summarise(a,s,self.now)
        self.assertEqual('FAILURE',o['streams']['TENANT_BILL']['result'])
        self.assertEqual('EG_TENANT_AUTH_FAILED',o['streams']['TENANT_BILL']['support_ref'])

    def test_blocked_stream_hides_bill_name(self):
        a,s=core_reports();s['streams']['EB_BILL']={'state':'BLOCKED','support_ref':'EG_NATIVE_SYNC_FOLDER_UNSAFE',
            'invoices':[{'display_filename':'private-tenant-personal-name.pdf','drive_upload':{'state':'HOLD'}}]}
        o=summarise(a,s,self.now);encoded=json.dumps(o)
        self.assertEqual('FAILURE',o['streams']['EB_BILL']['result'])
        self.assertRegex(o['streams']['EB_BILL']['incident_ref'],r'^bill-[0-9a-f]{24}$')
        self.assertNotIn('personal-name',encoded)

    def test_incomplete_not_reported_ok(self):
        a,s=core_reports();s['streams']['TENANT_BILL']['state']='PENDING'
        o=summarise(a,s,self.now)
        self.assertEqual('PENDING',o['streams']['TENANT_BILL']['result'])
        self.assertEqual('EG_MONITOR_PENDING_AFTER_RUN',o['streams']['TENANT_BILL']['support_ref'])

    def test_unknown_failed_reason_is_closed(self):
        a,s=core_reports();a['acquisition']['EB_BILL']={'status':'ERROR','support_ref':'password = secret'}
        o=summarise(a,s,self.now)
        self.assertEqual('EG_MONITOR_ACQUISITION_FAILED',o['streams']['EB_BILL']['support_ref'])
        self.assertNotIn('secret',json.dumps(o))

    def test_missing_stream_refuses(self):
        a,s=core_reports();del a['acquisition']['EB_BILL']
        with self.assertRaises(ReportError):summarise(a,s,self.now)

    def test_invalid_uuid_refuses(self):
        a,s=core_reports();a['run_id']='oops'
        with self.assertRaises(ReportError):summarise(a,s,self.now)

    def test_private_file_limit_and_symlink_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x.json';p.write_text('{}',encoding='utf-8')
            self.assertEqual({},_json_file(p))
            p.write_bytes(b'x'*(512*1024+1))
            with self.assertRaises(ReportError):_json_file(p)
            p.write_text('{}',encoding='utf-8')
            link=Path(d)/'link.json'
            try:link.symlink_to(p)
            except OSError:pass
            else:
                with self.assertRaises(ReportError):_json_file(link)

    def test_local_endpoint_only(self):
        _validate_endpoint('http://127.0.0.1:5678/webhook/xb-energygrid-monitor-v1')
        _validate_endpoint('http://localhost:5678/webhook/xb-energygrid-monitor-v1')
        for u in ['http://host.docker.internal:5678/webhook/xb-energygrid-monitor-v1',
                  'http://evil.tld:5678/webhook/xb-energygrid-monitor-v1',
                  'https://127.0.0.1:5678/webhook/xb-energygrid-monitor-v1',
                  'http://127.0.0.1:5678/webhook/xb-energygrid-monitor-v1?token=abc',
                  'http://127.0.0.1:5678/webhook/other',
                  'http://username:secret@localhost:5678/webhook/xb-energygrid-monitor-v1']:
            with self.subTest(url=u),self.assertRaises(ReportError):_validate_endpoint(u)

    def test_unknown_drive_state_refuses_instead_of_reporting(self):
        a,s=core_reports();s['streams']['EB_BILL']['state']='SOMETHING_NEW'
        with self.assertRaisesRegex(ReportError,'EG_MONITOR_DRIVE_STATE_INVALID'):summarise(a,s,self.now)


def core_files(run_dir, acquire_at=None, status_at=None):
    a,s=core_reports()
    a.update(schema=CORE_SCHEMA,command='acquire',outcome='ACQUIRED')
    s.update(schema=CORE_SCHEMA,command='status',outcome='DRAINED')
    for name,obj,at in (('acquire.json',a,acquire_at),('status.json',s,status_at)):
        path=run_dir/name;path.write_text(json.dumps(obj),encoding='utf-8')
        if at is not None:os.utime(path,(at.timestamp(),at.timestamp()))


class RunDirBindingTests(unittest.TestCase):
    """acquire.json and status.json must come from one fresh egnative new-run folder, reported once."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'runs';self.root.mkdir()
        self.started=datetime.now().replace(microsecond=0)-timedelta(minutes=5)
        self.run=self.root/self.started.strftime('%Y%m%d-%H%M%S');self.run.mkdir()

    def test_current_run_binds(self):
        core_files(self.run)
        a,s=bind_run_dir(self.run,self.root)
        self.assertEqual('OK',summarise(a,s)['streams']['EB_BILL']['result'])

    def test_folder_outside_runs_root_refused(self):
        other=Path(self.tmp.name)/self.run.name;other.mkdir();core_files(other)
        with self.assertRaisesRegex(ReportError,'EG_MONITOR_RUN_DIR_REFUSED'):bind_run_dir(other,self.root)

    def test_old_run_folder_refused(self):
        old=self.root/(self.started-timedelta(hours=8)).strftime('%Y%m%d-%H%M%S');old.mkdir();core_files(old)
        with self.assertRaisesRegex(ReportError,'EG_MONITOR_RUN_NOT_CURRENT'):bind_run_dir(old,self.root)

    def test_status_older_than_acquire_refused(self):
        core_files(self.run,acquire_at=self.started+timedelta(minutes=3),status_at=self.started+timedelta(minutes=1))
        with self.assertRaisesRegex(ReportError,'EG_MONITOR_RUN_FILES_NOT_CURRENT'):bind_run_dir(self.run,self.root)

    def test_file_older_than_run_refused(self):
        core_files(self.run,acquire_at=self.started-timedelta(days=1))
        with self.assertRaisesRegex(ReportError,'EG_MONITOR_RUN_FILES_NOT_CURRENT'):bind_run_dir(self.run,self.root)

    def test_missing_status_refused(self):
        core_files(self.run);(self.run/'status.json').unlink()
        with self.assertRaises(ReportError):bind_run_dir(self.run,self.root)

    def test_wrong_command_refused(self):
        core_files(self.run);s=json.loads((self.run/'status.json').read_text(encoding='utf-8'));s['command']='next'
        (self.run/'status.json').write_text(json.dumps(s),encoding='utf-8')
        with self.assertRaisesRegex(ReportError,'EG_MONITOR_NOT_CORE_STATUS'):bind_run_dir(self.run,self.root)

    def test_refused_acquire_is_not_reported(self):
        core_files(self.run);a=json.loads((self.run/'acquire.json').read_text(encoding='utf-8'));a['outcome']='REFUSED'
        (self.run/'acquire.json').write_text(json.dumps(a),encoding='utf-8')
        with self.assertRaisesRegex(ReportError,'EG_MONITOR_ACQUIRE_NOT_COMPLETED'):bind_run_dir(self.run,self.root)

    def test_run_reported_at_most_once_without_retry(self):
        core_files(self.run)
        token=Path(self.tmp.name)/'token.txt';token.write_text('a'*48,encoding='utf-8')
        argv=['--run-dir',str(self.run),'--runs-root',str(self.root),'--token-file',str(token),
              '--endpoint','http://127.0.0.1:9/webhook/xb-energygrid-monitor-v1']  # nothing listens: no network effect
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(20,main(argv))
        self.assertIn('EG_MONITOR_LOCAL_WEBHOOK_UNAVAILABLE',err.getvalue())
        self.assertEqual('xb.energygrid.monitor.v1',json.loads((self.run/'monitor-report.json').read_text(encoding='utf-8'))['schema'])
        self.assertEqual({'outcome':'EG_MONITOR_LOCAL_WEBHOOK_UNAVAILABLE'},
                         json.loads((self.run/'monitor-post.json').read_text(encoding='utf-8')))
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(20,main(argv))
        self.assertIn('EG_MONITOR_RUN_ALREADY_REPORTED',err.getvalue())

    def test_run_dir_excludes_explicit_files(self):
        core_files(self.run)
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(20,main(['--run-dir',str(self.run),'--acquire-file',str(self.run/'acquire.json'),'--dry-run']))
        self.assertIn('EG_MONITOR_ARGUMENTS_REFUSED',err.getvalue())

if __name__=='__main__':unittest.main()
