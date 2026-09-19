# Replay transcript — 20260918-183211
- **Capability**: lookup_member_balance
- **Status**: approved
- **Product**: CoreBankPro@4
- **Entry URL**: http://127.0.0.1:8000/members.html?slow=2500
- **Parameters**: {'params.member_id': '***', 'params.branch_code': '001'}
- **Model in the loop**: none

## open_application — navigate

- **Checkpoint**: passed after 22 ms

## type_1 — type

- **Resolved at**: rung 1
- **Checkpoint**: passed after 6 ms

## type_2 — type

- **Resolved at**: rung 2
- **Checkpoint**: passed after 10 ms

## click_3 — click

- **Resolved at**: rung 1
- **Checkpoint**: passed after 2558 ms

## read_savings_balance — read

- **Resolved at**: rung 2
- **Checkpoint**: passed after 1 ms
- **Read**: $2,755.55

## Result

- **Classification**: success
- **Elapsed**: 2851 ms
- **Outputs**: {'savings_balance': '$2,755.55'}
