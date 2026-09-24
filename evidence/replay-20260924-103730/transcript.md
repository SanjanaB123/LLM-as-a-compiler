# Replay transcript — 20260924-103730
- **Capability**: lookup_member_balance
- **Status**: approved
- **Product**: CoreBankPro@4
- **Entry URL**: http://127.0.0.1:8000/members.html?session_expires=1
- **Parameters**: {'params.member_id': '***', 'params.branch_code': '001'}
- **Model in the loop**: none

## open_application — navigate

- **Checkpoint**: passed after 2 ms

## type_1 — type

- **Resolved at**: rung 1
- **Checkpoint**: passed after 3 ms

## type_2 — type

- **Resolved at**: rung 2
- **Checkpoint**: passed after 9 ms

## click_3 — click

- **Resolved at**: rung 1
- **Checkpoint**: passed after 1 ms
- **ERROR**: The application signed us out mid-flow. Re-authenticating requires credentials the automation is never permitted to handle, so this cannot be recovered in-loop and escalates to a person.

## Result

- **Classification**: hard_failure
- **Elapsed**: 586 ms
- **Failed step**: click_3
- **Expected**: the search returned a response: either a member detail panel or an alert
- **Observed**: The application signed us out mid-flow. Re-authenticating requires credentials the automation is never permitted to handle, so this cannot be recovered in-loop and escalates to a person.
- **Screenshot**: evidence/replay-20260924-103730/failure-click_3.png
