# Cloud handoff: Reelay final sprint

Work on `hackathon-all-nov4`. Preserve `pre-hackathon` and distinguish the existing desktop foundation from the Repurpose Engine and subsequent maintenance fixes. Andrew submits through Cerebral Valley himself. His November 4, 2026 target is 4 PM Pacific; supplied hard cutoff is 6 PM Pacific. Check the current time before making a plan.

## Current behavior and fixes

The Muse Spark engine, desktop button, consent/source snapshot, proposal review, and explicit Apply then Save Changes flow are implemented. Existing committed fixes address completed-worker lifetime errors and Threads hashtags after refinement.

The latest maintenance changes address repeated source listings:

- `reelay/gui/download_page.py`: restart pending date/product lookups after interrupted workers finish; reject metadata from an outdated listing generation/profile; refresh ordering after metadata arrives; suppress restarts during shutdown, listing, or downloads.
- `reelay/gui/table_model.py`: unknown dates stay last for both newest-first and oldest-first ordering.
- `tests/test_listing_refresh.py`: five regression cases covering late dates across platforms, shared list/grid pagination, repeated listing handover, product lookup handover, and stale profile metadata.

These fixes address listing and ordering defects. They do not establish that the native crash described below is eliminated.

## Runtime crash and workaround

On Andrew's Mac, repeated listings from multiple sources crashed Python 3.14.0 with PySide6 6.11.2. The native report identified `ThreadPoolExecutor-0_2`, `mark_stacks`, and `_PyGC_Collect`, while the main thread was painting. The root cause is not conclusively established.

A separate local Python 3.13.7 environment, retaining PySide6 6.11.2 and the main dependency versions, passed dependency checks and local tests. Andrew authorized access to his existing Reelay Keychain item. This is a workaround pending a successful real-data walkthrough, not a confirmed permanent crash fix.

The Mac-only environment was created at `/tmp/reelay-py313`. It is temporary, is not committed, and is NOT available in a cloud checkout. The original Mac `.venv` uses Python 3.14.0.

For a fresh checkout, create an environment with Python 3.13 and install the project plus test dependencies and `requests` (currently imported but not explicitly declared by the project):

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]' requests 'PySide6-Essentials==6.11.2' 'PySide6-Addons==6.11.2'
```

Use `QT_QPA_PLATFORM=offscreen` for cloud/headless GUI tests. Report unavailable platform prerequisites honestly. The macOS application launches with `python -X faulthandler -m reelay` from the selected environment.

## Verification already performed

- All five listing regression cases pass.
- Ninety-three other relevant existing cases passed. Two date tests in the broader run initially used an incorrect mixed-platform fixture; it was corrected and both passed on rerun. This was not one uninterrupted 98-test green run.
- A native macOS window test loading metadata for 120 synthetic videos while rendering passed after these fixes. Its temporary local test script is not in this cloud checkout.
- Earlier focused generation, privacy, and GUI tests pass; a real CLI API call succeeded earlier in the session.
- Andrew has not yet completed a successful full GUI walkthrough on his actual videos. Do not equate mocked tests with that walkthrough.

Reproducible repository checks:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_listing_refresh.py -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_sources.py tests/test_products.py tests/test_library_columns.py tests/test_gui.py tests/test_repurpose_engine.py tests/test_privacy.py tests/test_repurpose_gui.py -q
```

Do not copy API keys into the repository, logs, or handoff. A cloud checkout cannot read Andrew's Mac Keychain. Networkless tests use synthetic credentials and mocked generation. Some older broad GUI tests can start background lookups; they are not evidence of a successful live Muse Spark GUI call. A background media-probe closed-database error has appeared in earlier test teardown; establish its provenance rather than assuming it is fixed.

## Finish-line tasks

1. Inspect the branch and run relevant checks. Fix only demonstrated blockers, then freeze scope.
2. Determine whether your cloud environment can run a desktop interface. If it cannot operate Andrew's Mac, say so and guide him one screen at a time. Do not claim you tested a native macOS flow remotely.
3. Walk Andrew through listing sources repeatedly, date sorting, selecting his own video, checking the Source Snapshot, blocked generation with consent unchecked, a real explicitly authorized generation, nine-field review, regeneration, cancellation, Apply without persistence, and explicit Save Changes on a suitable test ticket. Do not schedule or publish.
4. Give short numbered instructions, expected outcomes, and clear failure-reporting steps.
5. Andrew asks: **Can you create the required demo video using his Overnight Editor tool?** Inspect available integrations/project access and answer what you can actually do. Overnight Editor is a separate local tool/project, not included in this repository. Ask for its repository/service or footage if that is required; do not assume a cloud checkout controls it. Help produce an MP4 under three minutes from real app footage if supported. Specify any required footage/narration; do not fabricate a working interface or successful operation.
6. Make the public code, documentation, demo, and submission consistent. SEO is pre-existing; its 0–100 score is a local heuristic informed by YouTube suggestions, not a ranking prediction. Privacy covers explicit cloud generation, not fully offline operation. Do not claim perfect factuality or complete prompt-injection immunity.
7. Report the exact tested/pushed commit, remaining manual checks, video capability/result, and final submission checklist. Andrew performs the final Cerebral Valley submission.
