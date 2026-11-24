# Dashboard updates

Supply the spreadsheet tab IDs in the `sheet_ids` argument to `mirror_prepare` or in `dashboard-sheet-ids.json` inside the private directory. Configure the actual integer tab ID for Today, Jobs, Applied, History, Recruiters, Companies and Rejected. Obtain those IDs from the operator's spreadsheet metadata.

Read the affected cells, prepare the local diff and verify its base with `mirror_verify`. Apply the planned ranges. Read the cells back and verify every applied batch. Refresh a stale plan after any concurrent user edit. Keep Settings under the operator's control.
