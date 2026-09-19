# Replay transcript — 20260918-203021
- **Capability**: open_sub_account
- **Status**: approved
- **Product**: CoreBankPro@4
- **Entry URL**: http://127.0.0.1:8000/admin.html
- **Parameters**: {'params.member_id': '***', 'params.branch_code': '001', 'params.initial_deposit': '500'}
- **Model in the loop**: none

## open_application — navigate

- **Checkpoint**: FAILED after 0 ms
- **ERROR**: navigation to '/admin.html' is outside the permitted routes ['/members.html'] — the origin is allowed but this page is not

## Result

- **Classification**: refused
- **Elapsed**: 1 ms
- **Refused at**: open_application
- **Reason**: navigation to '/admin.html' is outside the permitted routes ['/members.html'] — the origin is allowed but this page is not
- **Policy**: origins=['http://127.0.0.1:8000'] routes=['/members.html'] actions=['click', 'navigate', 'read', 'type']
