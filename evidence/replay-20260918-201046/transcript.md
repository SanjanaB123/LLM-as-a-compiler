# Replay transcript — 20260918-201046
- **Capability**: lookup_member_balance
- **Status**: approved
- **Product**: CoreBankPro@4
- **Entry URL**: http://127.0.0.1:8000/members.html?drift=2
- **Parameters**: {'params.member_id': '***', 'params.branch_code': '001'}
- **Model in the loop**: none

## open_application — navigate

- **Checkpoint**: FAILED after 0 ms
- **ERROR**: url=http://127.0.0.1:8000/members.html?drift=2 | controls present: textbox 'Member ID', textbox

## Result

- **Classification**: hard_failure
- **Elapsed**: 8278 ms
- **Failed step**: open_application
- **Expected**: button 'Search' is on screen
- **Observed**: url=http://127.0.0.1:8000/members.html?drift=2 | controls present: textbox 'Member ID', textbox
- **Screenshot**: evidence/replay-20260918-201046/failure-open_application.png
