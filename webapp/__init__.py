"""
webapp/

The long-term goal this project is building toward: a browsable view of
"what did each AI think vs. what Kalshi priced it at" for every tracked
market. Reads predictions.db directly (see Tracker.py) -- the one table
DailyLog.py and Collect.py both write into.

    py -m uvicorn webapp.main:app --reload

This is a first framework pass, not a finished product: admin-created user
accounts (see auth.py and Accounts.py), no pagination, server-rendered HTML with a couple of JSON endpoints alongside
for whatever comes next (a JS frontend, a mobile client).
"""
