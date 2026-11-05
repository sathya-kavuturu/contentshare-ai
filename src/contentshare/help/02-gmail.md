# Connect Gmail

ContentShare signs in with **Google's own sign-in page**. It never sees or stores your password.
You do the Google Cloud steps **once**, signed in as the account that receives the requests
(about 10–15 minutes). Google renames menus now and then; if a name differs, look for the
closest one.

## 1. Create a Google Cloud project

1. Open the [Google Cloud console](https://console.cloud.google.com) signed in as the
   mailbox account.
2. Create a project, for example **ContentShare**.

## 2. Turn on the Gmail API

*APIs & Services → Library → Gmail API → Enable.*

## 3. Set up the consent screen

*APIs & Services → OAuth consent screen* (newer consoles call it **Google Auth Platform**).

* **User type: Internal.** This is for apps used only inside your Google Workspace
  organisation. No Google review is needed and the sign-in does not expire.
* If *Internal* is greyed out (the account is not in a Workspace organisation) you must choose
  *External*. In *Testing* mode Google ends the sign-in **every 7 days**, so you would have to
  connect again weekly. Ask your Workspace admin about Internal instead.

## 4. Create the OAuth client

*APIs & Services → Credentials → Create credentials → OAuth client ID.*

* **Application type: Desktop app.** Name it anything.
* **Download the JSON file.** Save it somewhere safe **outside any project or git folder**, for
  example `%APPDATA%\ContentShare\client_secret.json`. Never email it or commit it.

## 5. Admin approval (Workspace only)

If sign-in says the app is blocked, a Workspace admin must allow it in the *Admin console →
Security → Access and data control → API controls → App access control* (add the OAuth client
ID). Internal apps are normally allowed already.

## 6. Connect in ContentShare

1. Open **Settings → Gmail**.
2. Next to **Client file**, press **Browse…** and choose the JSON file you saved.
3. Press **Connect Gmail…**. ContentShare saves your settings, then your browser opens
   Google's sign-in.
4. Choose the mailbox account and **approve every permission**. If one is unticked ContentShare
   refuses the sign-in rather than run half-working.
5. Back in ContentShare the status reads **✔ Connected** and the "Gmail is not connected" bar
   disappears.

The sign-in is kept only in **Windows Credential Manager** (entries named `ContentShare`),
never in a file.

## What each permission is for

| Permission | Used for |
|---|---|
| `gmail.modify` | Reading request emails, and adding the **ContentShareAI/Processed** label. It cannot delete mail permanently, and ContentShare only ever adds its own label. |
| `gmail.send` | Sending the "content shared" reply in the same thread. |
| `gmail.readonly` | Used instead of `gmail.modify` if you turn **Label processed emails** off. |

If you would rather give it less access, turn the label option off in *Settings → Gmail*,
save, **Disconnect**, and connect again.

## Trying it safely

With **dry-run mode** on (*Settings → General*) you can connect, press **Check now** and look at
the cards without anything being copied or sent. Look at the sender line on each card: it says
whether Gmail confirmed the sender.

If real mail from your own organisation shows as *unverified*, some internal mail carries no
authentication result. You can untick **Treat a sender as verified only if Gmail confirms it**,
but only if you accept that a forged "From" address could then be treated as genuine.

## Disconnecting

*Settings → Gmail → Disconnect.* This revokes access at Google and deletes the saved sign-in
from this PC. You can also remove ContentShare at
[Google account permissions](https://myaccount.google.com/permissions).

[Back to Getting started](help:start)
