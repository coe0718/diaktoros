"""``python -m diaktoros.run_supervisor``: the worker and the operator commands (#717).

The package's one module runs here, imported: not a second ``__main__`` copy of it beside the
imported one, so its classes (``LedgerMissing``, ``Supervisor``, ...) have one identity in a worker.
"""
from . import main

main()
