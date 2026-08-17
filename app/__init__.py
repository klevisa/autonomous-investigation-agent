"""AIA app package.

No sys.path shim needed: `lib/` is a proper package (has `lib/__init__.py`) and the app deploys the repo
root as its source_code_path, so `from lib.pg import ...` resolves natively from the source tree. (The jobs
install lib/ as a wheel; both runtimes use package imports. See pyproject.toml.)
"""
