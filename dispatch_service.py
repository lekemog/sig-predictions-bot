"""Persistent trigger for the sole GitHub executor. Never calls the SIG API.

Default dry-run. Operators activate live mode on the deployment host. All
dispatch attempts are journaled before the POST; unknown outcomes stop new
dispatches and are never blindly retried. GitHub's existing concurrency group
continues to serialize execution and durable order recovery.
"""
import argparse
import base64
import fcntl
import json
import os
import signal
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

from decision_engine import timestamp
from trading_bot import END, Halt, now

REPOSITORY='lekemog/sig-predictions-bot'
WORKFLOW='sig-management.yml'
ACTIVE=('queued','in_progress','waiting','pending','requested')


class GitHub:
    def __init__(self, token):
        if not token:
            raise Halt('GitHub dispatch credential missing')
        self.headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
                      'X-GitHub-Api-Version':'2022-11-28','Content-Type':'application/json'}
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args):return None
        self.opener=urllib.request.build_opener(NoRedirect)
    def request(self, method, path, payload=None):
        # Fixed repository; no arbitrary destinations or redirect credential leakage.
        if method not in ('GET','POST') or not path.startswith('/') or '..' in path:
            raise Halt('Invalid GitHub scheduler request')
        if method=='POST' and path!=f'/actions/workflows/{WORKFLOW}/dispatches':
            raise Halt('Scheduler can only dispatch the management workflow')
        request=urllib.request.Request(f'https://api.github.com/repos/{REPOSITORY}'+path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=self.headers,method=method)
        # Single network attempt, including POST. Exception text is never logged.
        with self.opener.open(request,timeout=30) as response:
            body=response.read()
            return json.loads(body) if body else {}
    def status(self):
        workflow=self.request('GET',f'/actions/workflows/{WORKFLOW}')
        if workflow.get('state')!='active':raise Halt('Management workflow disabled')
        response=self.request('GET','/contents/.bot/state.json?ref=main')
        checkpoint=json.loads(base64.b64decode(response['content'],validate=False))
        if (checkpoint.get('version')!=1 or checkpoint.get('enabled') is not True
                or not isinstance(checkpoint.get('jobs'),list)):
            raise Halt('Repository checkpoint disabled or invalid')
        for status in ACTIVE:
            result=self.request('GET',f'/actions/runs?status={status}&per_page=100')
            rows=result['workflow_runs']
            if result['total_count']>len(rows):raise Halt('Active run listing incomplete')
            if rows:return checkpoint, True
        return checkpoint, False
    def find_dispatch(self, trigger_id):
        result=self.request('GET',f'/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=100')
        return any(r.get('display_title')==f'SIG sweep [{trigger_id}]'
                   for r in result['workflow_runs'])
    def dispatch(self,mode,trigger_id):
        return self.request('POST',f'/actions/workflows/{WORKFLOW}/dispatches',
                            dict(ref='main',inputs=dict(mode=mode,trigger_id=trigger_id)))


class DispatchJournal:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, at TEXT, mode TEXT, status TEXT)')
        self.db.commit()
    def latest(self):
        return self.db.execute('SELECT at,mode,status,id FROM attempts ORDER BY id DESC LIMIT 1').fetchone()
    def begin(self,at,mode):
        with self.db:
            row=self.db.execute('INSERT INTO attempts(at,mode,status) VALUES (?,?,?)',
                                (at.isoformat(),mode,'unknown'))
        return row.lastrowid
    def finish(self,identity,status):
        with self.db:self.db.execute('UPDATE attempts SET status=? WHERE id=?',(status,identity))


def tick(api,journal,mode,at=None,interval=1200):
    at=at or now()
    if mode not in ('dry-run','live') or interval<1200:
        raise Halt('Invalid dispatch mode or interval')
    if at>=END:return dict(status='CUTOFF')
    last=journal.latest()
    if last:
        if last[2]=='unknown':
            trigger_id=f'sig-trigger-{last[3]}-{int(timestamp(last[0]).timestamp())}'
            if api.find_dispatch(trigger_id):
                journal.finish(last[3],'accepted')
                return dict(status='DISPATCH_OBSERVED_AFTER_UNKNOWN')
            return dict(status='UNKNOWN_DISPATCH_REQUIRES_REVIEW')
        age=(at-timestamp(last[0])).total_seconds()
        if age<0:raise Halt('Scheduler clock moved backwards')
        if age<interval:return dict(status='COOLDOWN')
    checkpoint,busy=api.status()
    if busy:return dict(status='EXECUTOR_BUSY')
    marker=checkpoint.get('last_successful_live_sweep' if mode=='live' else 'last_successful_sweep')
    if marker and marker.get('mode')==mode and marker.get('status')=='success':
        age=(at-timestamp(marker['at'])).total_seconds()
        if age<0:raise Halt('Future execution heartbeat')
        if age<interval:return dict(status='SWEEP_FRESH')
    # The exact pending order journal is recovered by GitHub, never by this worker.
    identity=journal.begin(at,mode)
    try:
        api.dispatch(mode,f'sig-trigger-{identity}-{int(at.timestamp())}')
    except urllib.error.HTTPError as error:
        # A definitive rejection can cool down; ambiguous server errors stay unknown.
        if error.code in (400,401,403,404,422):journal.finish(identity,'rejected')
        return dict(status='DISPATCH_REJECTED' if error.code in (400,401,403,404,422)
                    else 'UNKNOWN_DISPATCH_REQUIRES_REVIEW')
    except (OSError,ValueError):
        return dict(status='UNKNOWN_DISPATCH_REQUIRES_REVIEW')
    journal.finish(identity,'accepted')
    return dict(status='DISPATCH_ACCEPTED',mode=mode,
                note='Trigger acceptance is not proof of a successful live sweep')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--once',action='store_true')
    args=p.parse_args()
    if now()>=END:return
    mode=os.environ.get('DISPATCH_MODE','dry-run')
    if mode not in ('dry-run','live'):raise SystemExit('Invalid dispatch mode')
    os.umask(0o077);args.database.parent.mkdir(parents=True,exist_ok=True)
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    with Path(str(args.database)+'.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('Dispatch service already running') from None
        journal=DispatchJournal(args.database)
        try:
            api=GitHub(os.environ.get('GITHUB_DISPATCH_TOKEN'))
            while not stop.is_set() and now()<END:
                try:result=tick(api,journal,mode)
                except (Halt,OSError,KeyError,TypeError,ValueError):
                    result=dict(status='READ_OR_CONFIGURATION_UNAVAILABLE')
                print(json.dumps(dict(at=now().isoformat(),**result)),flush=True)
                if args.once:break
                stop.wait(min(60,max(0,(END-now()).total_seconds())))
        finally:journal.db.close()


if __name__=='__main__':main()
