"""Host-side native storage sweeps and explicit uncertain-run reconciliation."""
import subprocess
import sys
import time

from .. import util


def sweep_native(worker):
    from . import workers
    if sys.platform == 'darwin' and not worker.fixture_mode and not workers.in_worker():
        from .. import native_cleanup
        for name in ('native-cleanup', 'native-proof-cleanup'):
            root = worker.db.parent.resolve() / name
            attribute = name.replace('-', '_')
            setattr(worker, attribute + '_parent', root)
            if not (root.exists() or root.is_symlink()):
                continue
            try:
                result = native_cleanup.bounded_sweep(root)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                result = {'status': 'unverified', 'results': [], 'reason': str(exc)}
                util.log(f'{name} sweep unverified; receipts retained: {exc}')
            setattr(worker, attribute + '_results', result)


def reconcile(worker, run_id: str, *, reason: str, acknowledge_no_live_worker: bool,
              acknowledge_native_descendants_stopped: bool) -> bool:
    """Operator attestation, never automatic native descendant-exit proof.

    A present or inaccessible PID conservatively blocks release. A missing
    PID still requires acknowledgement: absence alone cannot prove that
    a GitHub write did not happen before the worker died.
    Native release also requires the operator to stop all descendants or
    reboot, then attest to that fact; this is not automatic exit proof.
    """
    from . import workers
    if not acknowledge_no_live_worker or not reason or len(reason) > 512:
        raise ValueError('explicit reconciliation acknowledgement and reason required')
    with worker._connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("SELECT pid,state,launch_intent,repo,pr,head,seat FROM runs WHERE id=?",
                          (run_id,)).fetchone()
        if row is None or row['state'] != 'uncertain':
            con.execute('COMMIT')
            return False
        native = sys.platform == 'darwin' and not worker.fixture_mode
        if native and not acknowledge_native_descendants_stopped:
            raise ValueError('native process completion unverified; explicit native '
                             'descendants stopped acknowledgement required')
        if row['pid'] is not None:
            found = workers.process_state(row['pid'], row['launch_intent'])
            if found == 'unknown':
                raise ValueError('cannot establish worker is absent')
            if found == 'alive':
                raise ValueError('worker PID exists; cannot release uncertain run')
        if row['launch_intent'] is None:
            raise ValueError('missing launch intent; cannot establish worker identity')
        con.execute("UPDATE runs SET state='failed', error=?, lease=NULL, "
                    "push_intent=NULL,updated=? "
                    "WHERE id=? AND state='uncertain'",
                    ('operator reconciliation: ' + reason +
                     ('; native descendants manually acknowledged stopped' if native else ''),
                     time.time(), run_id))
        con.execute('COMMIT')
    # The operator's reconciliation is what ends it, so it frees its claim too.
    workers._release_claim(row, run_id)
    return True
