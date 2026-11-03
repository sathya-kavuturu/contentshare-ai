# Connecting ContentShare to Gmail

ContentShare signs in with Google's own sign-in page (OAuth 2.0). It never sees or stores your
password. You do the Google Cloud steps once, on the account that receives the requests.

> These steps were written from Google's documentation and could not be tried against your
> account. Google renames menus now and then; if a name differs, look for the closest one.

## 1. Create a Google Cloud project

1. Open <https://console.cloud.google.com> signed in as your organisation account.
2. Create a project, for example **ContentShare**.

## 2. Turn on the Gmail API

**APIs & Services → Library → Gmail API → Enable.**

## 3. Set up the consent screen

**APIs & Services → OAuth consent screen** (newer consoles call it *Google Auth Platform*).

* **User type: Internal.** This is for apps used only inside your Google Workspace
  organisation. No Google review is needed and sign-ins do not expire after a week.
* If **Internal** is greyed out (your account is not part of a Workspace organisation), you must
  choose *External*. In *Testing* mode Google expires the sign-in **every 7 days**, so you would
  have to reconnect weekly. Ask your Workspace admin about Internal instead.
* Add the permissions (scopes) listed in the table below.

## 4. Create the OAuth client

**APIs & Services → Credentials → Create credentials → OAuth client ID.**

* **Application type: Desktop app.** Name it anything.
* **Download the JSON file** and save it somewhere safe **outside any project or git folder**,
  for example `%APPDATA%\ContentShare\client_secret.json`.
* Put its path in the settings file under `gmail.client_secret_path`.

The file identifies the app, not you. Still, never commit or email it.

## 5. Admin approval (Workspace)

If sign-in says the app is blocked, a Workspace admin must allow it under
**Admin console → Security → Access and data control → API controls → App access control**
(add the OAuth client ID). Internal apps are normally allowed by default; some organisations
restrict Gmail permissions anyway.

## 6. Connect

```powershell
python -m contentshare.cli gmail-connect
```

Your browser opens Google's sign-in. Choose the mailbox account and approve **every** permission
(unticking one makes ContentShare refuse the sign-in rather than run half-working).
The token is saved only in **Windows Credential Manager** (Control Panel → Credential Manager →
Windows Credentials, entries named `ContentShare` / `gmail-oauth.*`), never in a file.

## 7. Check what ContentShare thinks of your real mail (read-only)

```powershell
python -m contentshare.cli gmail-peek --limit 10
```

This lists recent mail from your department senders and whether each sender would be accepted
as **verified**, **suspicious** or **unknown**, with the reason. It saves nothing and changes
nothing in Gmail. Run it before trusting the automatic checks: it shows what Gmail's
`Authentication-Results` headers look like on *your* mail. If genuine mail from your own
organisation shows as `unknown` (some internal mail carries no results), see the note below.

## Disconnecting

```powershell
python -m contentshare.cli gmail-disconnect
```

This revokes access at Google and deletes the saved sign-in. You can also remove ContentShare
at <https://myaccount.google.com/permissions>.

## What each permission is for

| Permission | Needed for | Used when |
|---|---|---|
| `gmail.readonly` | Reading request emails | Only if you turn **labels off** (`gmail.apply_label: false`) |
| `gmail.modify` | Reading, and adding the `ContentShare/Processed` label | Default (labels on). It also allows other changes to mail (but not permanent deletion); ContentShare only ever adds its own label |
| `gmail.send` | Sending the "content shared" reply | Always |

ContentShare asks for **`gmail.modify` + `gmail.send`** by default, or **`gmail.readonly` +
`gmail.send`** if you set `gmail.apply_label: false`. Labels are a convenience: the app's own
database already makes sure no email is processed twice. If you would rather give it less
access, turn labels off and reconnect. It does not request full access to your mailbox.

## About sender checks

An email is trusted as coming from the right person only if Gmail recorded that the sender
passed **DMARC** (or SPF *and* DKIM) for the sender's own domain, and only results written by
a server listed in `gmail.trusted_authserv_ids` (default `mx.google.com`) are believed. Anything
else is marked **Suspicious** or **Unknown** and always goes to manual review.

If genuine internal mail shows as *unknown*, you can set `gmail.require_authentication: false`.
Do this only if you accept that a forged "From" address could then be treated as genuine.
