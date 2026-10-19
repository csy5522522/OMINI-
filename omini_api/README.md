# Omini API integration

This package contains the 16 Python modules from the existing local Omini API
toolbox. Internal imports use the `omini_api` namespace. GUI and worker processes
run through `runtime.py`; both source and frozen builds use the same dispatcher.

Account records, site profiles, mailbox credentials, state and logs live in the
selected data directory. No personal data files are included in this package.
The existing seven Tk pages retain their original behavior and run in a separate
process embedded in the desktop page on Windows.

The upstream HTTP fixture and proxy-path regressions live in `tests/omini_api/`.
