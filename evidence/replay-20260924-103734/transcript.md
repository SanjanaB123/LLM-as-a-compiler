# Replay transcript — 20260924-103734
- **Capability**: lookup_member_balance
- **Status**: approved
- **Product**: CoreBankPro@4
- **Entry URL**: http://127.0.0.1:8000/members.html?blocker=1
- **Parameters**: {'params.member_id': '***', 'params.branch_code': '001'}
- **Model in the loop**: none

## open_application — navigate

- **Checkpoint**: passed after 1 ms

## type_1 — type

- **Resolved at**: rung 1
- **Checkpoint**: passed after 4 ms

## type_2 — type

- **Resolved at**: rung 2
- **Checkpoint**: passed after 8 ms

## click_3 — click

- **Resolved at**: rung 1
- **Checkpoint**: FAILED after 8006 ms
- **ERROR**: url=http://127.0.0.1:8000/members.html?blocker=1 | heading=['Supervisor Override Required'] | controls present: button 'Search', button 'Override', textbox 'Member ID', textbox

## Result

- **Classification**: hard_failure
- **Elapsed**: 8574 ms
- **Failed step**: click_3
- **Expected**: the search returned a response: either a member detail panel or an alert
- **Observed**: url=http://127.0.0.1:8000/members.html?blocker=1 | heading=['Supervisor Override Required'] | controls present: button 'Search', button 'Override', textbox 'Member ID', textbox
- **Screenshot**: evidence/replay-20260924-103734/failure-click_3.png
