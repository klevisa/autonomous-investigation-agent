"""AIA shared library — packaged as the `aia-lib` wheel (see pyproject.toml).

Both runtimes import these as a PACKAGE (`from lib.pg import ...`):
  * the app: the repo root is on its path, so `lib.*` resolves from the source tree that ships with it;
  * the jobs: the wheel is installed into the serverless environment (databricks.yml), so `lib.*` resolves
    from site-packages — no reading lib/*.py off the /Workspace FUSE mount (which flaked), no sys.path shim.
Intra-package imports are therefore also package-style (`from lib.pg import ...`), NOT flat.
"""
