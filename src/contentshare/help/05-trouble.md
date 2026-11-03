# Troubleshooting

| Problem | What to do |
|---|---|
| **"Gmail is not connected" bar** | *Settings → Gmail → Connect Gmail…* See [Connect Gmail](help:gmail). If it keeps coming back every week, the Google app is in *Testing* mode: use an **Internal** consent screen (see the Gmail guide). |
| **Google says the app is blocked** | A Workspace admin must allow the OAuth client. See step 5 of [Connect Gmail](help:gmail). |
| **A request is Needs review but the match is obvious** | Choose it with **Use this**. To make such matches Ready, lower *Mark Ready when at least* in *Settings → Matching and copying*, but check real matches first. |
| **A program is not found** | *Settings → Index → Refresh index now*, and check the storage folders in *Settings → General*. |
| **Settings will not save** | Read the message: it names the entry. A copy running also blocks saving; try again when it finishes. |
| **A storage folder was added by mistake** | *Settings → General*: select it, then **Remove** (or **Edit** to correct it), then **Save settings**. |
| **A copy shows Failed again and again** | Read the reason. If files keep failing verification, test the PC's memory, the cable and the server, because the checksum is catching real corruption. |
| **No pop-ups** | Windows *Settings → System → Notifications*: make sure ContentShare is on and Focus is off. Also check *Show pop-up notifications* in ContentShare's settings. |
| **The window disappeared** | It is still running in the tray (next to the clock, maybe under the ^ arrow). Click the icon, or right-click → **Open ContentShare**. |
| **An update will not start** | Open the folder shown in the message and run the installer yourself, or download the newest one from the project's releases page. |

## Where things are kept

| What | Where |
|---|---|
| Settings | `%APPDATA%\ContentShare\config.yaml` (the previous version is kept as `config.yaml.bak`) |
| History and the file index | `%APPDATA%\ContentShare\contentshare.db` |
| Log | `%APPDATA%\ContentShare\logs\contentshare.log` (never contains passwords or email text) |
| Gmail sign-in | Windows Credential Manager, entries named `ContentShare` |
| Downloaded updates | `%APPDATA%\ContentShare\updates` |

## Moving to another PC

Install ContentShare, close it, copy `config.yaml` and `contentshare.db` into the same folder
on the new PC, start it and **connect Gmail again**. The sign-in is deliberately not copied.

## Uninstalling

*Windows Settings → Apps → ContentShare → Uninstall.* Your settings and history stay in
`%APPDATA%\ContentShare` unless you delete that folder. Disconnect Gmail first (*Settings →
Gmail → Disconnect*) so access is revoked at Google too.

## Privacy

* ContentShare reads only mail from the senders you list.
* Email text is never written to the log, and pop-ups show counts only.
* The only other internet contact is the optional start-up check for a new version (a plain read
  of the project's public releases page).

[Back to Getting started](help:start)
