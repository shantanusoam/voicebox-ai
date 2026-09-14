"""Explicit local data retention. Dry-run by default; --apply performs deletion."""
import argparse
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from callbox.config import Config

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--days',type=int,default=30);p.add_argument('--apply',action='store_true')
    args=p.parse_args()
    if args.days<1:p.error('Use at least one day.')
    config=Config.from_env()
    # Use SQLite directly so maintenance does not trigger startup call recovery.
    import sqlite3,json
    db=sqlite3.connect(config.data_dir/'callbox.db');db.execute('PRAGMA foreign_keys=ON')
    cutoff=(datetime.now(timezone.utc)-timedelta(days=args.days)).isoformat()
    ids=[r[0] for r in db.execute("SELECT id FROM calls WHERE workspace=? AND status='ended' AND started_at<?",(config.workspace,cutoff))]
    report={'dry_run':not args.apply,'cutoff':cutoff,'ended_calls':len(ids),'scope':'calls, transcripts, cached turn responses and old events; appointments and tasks retain their names until separately removed'}
    if args.apply:
        with db:
            for cid in ids:
                db.execute('DELETE FROM idempotency WHERE workspace=? AND scope IN (?,?,?)',(config.workspace,cid,'audio:'+cid,'ws-audio:'+cid))
                db.execute('DELETE FROM calls WHERE id=? AND workspace=?',(cid,config.workspace))
            db.execute('DELETE FROM events WHERE workspace=? AND created_at<?',(config.workspace,cutoff))
            db.execute('DELETE FROM sessions WHERE expires_at<?',(datetime.now(timezone.utc).timestamp(),))
    print(json.dumps(report,indent=2));db.close()

if __name__=='__main__':main()
