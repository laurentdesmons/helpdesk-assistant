---
category: support
doc_type: kb_article
title: VPN Connection Troubleshooting
last_updated: 2026-01-15
status: SAMPLE PLACEHOLDER — fictional content for pipeline testing only
---

# VPN Connection Troubleshooting

## Symptom
User cannot connect to the corporate VPN, or the connection drops repeatedly.

## Steps
1. Confirm the user is on a supported client version (VPN Client 4.x or later). Older versions are not supported and must be upgraded via Software Center.
2. Verify network connectivity outside the VPN (can the user reach the general internet?). If not, this is a local network issue, not a VPN issue.
3. Have the user restart the VPN client and reattempt connection.
4. Check if the user's account is locked or password expired — VPN auth fails silently in both cases. Direct user to password reset flow if applicable.
5. If the above fail, restart the machine's network adapter, then retry.

## Escalation
If none of the above resolve the issue within 2 attempts, escalate to Network Operations with the client log bundle (Help > Export Logs in the VPN client).

## Scope note
This article does not cover site-to-site VPN or vendor-specific hardware VPN appliances — those are handled by a separate network engineering runbook.
