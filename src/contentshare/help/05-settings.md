# Settings explained

Change things on any tab, then press **Save settings** (bottom right). **Revert** throws away
what you typed since the last save. If something you entered is not acceptable, a message says
which entry and why, and nothing is saved. Settings cannot be saved while a copy is running.

## General

| Setting | What it does |
|---|---|
| **Theme** | *Same as Windows*, *Light* or *Dark*. The window changes as soon as you choose, and the choice is remembered at once (you do not need to press Save). |
| **Check for requests automatically** | Lets ContentShare read new mail by itself. It only adds cards; it never copies or sends. |
| **How often** | *Once a day, at a set time*, or every 15 or 30 minutes, every hour, or every 2, 3, 6 or 12 hours. A shorter wait means a new request shows up sooner; each check only reads. |
| **Time of day** | For *Once a day*: the time of the check (24-hour clock). Not used when a check is made every so many minutes or hours. |
| **Check for requests when the app starts if a check was missed** | If the PC was off when a check was due, check as soon as ContentShare starts. |
| **Start ContentShare when I sign in to Windows** | Starts it quietly in the tray at sign-in. It adds one entry for your user only; switching it off removes it. |
| **Keep running in the tray when the window is closed** | ✕ hides the window instead of quitting, so the automatic check keeps working. Quit from the tray icon's menu. |
| **Show pop-up notifications** | Windows pop-ups with counts only, never names, subjects or folders. |
| **Tell a Telegram group when new requests wait for approval** | Posts one short message to a Telegram group each time a check finds new requests. See *Telegram alerts* below. |
| **Tick "send the reply automatically" on new requests** | The default for the tick box on each card. The reply is then sent as soon as the copy is verified. Leave it off until you trust the results. |
| **Dry-run mode** | Approving checks paths, free space and names and reports what would be copied, but **copies nothing and sends nothing**. A bar across the window reminds you while it is on. |
| **Storage folders to search** | The folders on the server that hold the programs (see below). |
| **Default reply text** | The message sent when a copy is verified. Departments can have their own. |
| **Check for updates when ContentShare starts** | See *Updates* below. |

### Telegram alerts

So the people who approve requests hear about them without keeping the window open.

1. In Telegram, open **@BotFather**, send `/newbot`, and copy the **token** it gives you.
2. Add the new bot to your group, then write any message in the group.
3. In *Settings → General → Telegram* tick the switch, paste the token, and press **Find my
   group** (it fills in the group number). Press **Send a test message** to check, then
   **Save settings**.

The message only gives counts, for example *"3 new requests waiting for approval (2 ready, 1 need
a look). Open ContentShare to review and approve. Nothing has been copied or sent."* It never
contains an email subject, a sender, a program name or a path. Approving stays with you: the
message only tells people that something is waiting.

The bot token is a password. It is kept in Windows Credential Manager, not in the settings file.
**Forget the token** removes it. If you ever shared it by mistake, send `/revoke` to @BotFather
to get a new one. Dry-run mode does not stop these alerts. If an alert cannot be sent, the status
bar says so and the requests are still shown as usual.

### Storage folders

* **Add folder…** opens the usual Windows folder picker.
* Or **type or paste a path** in the box and press **Add** (or Enter). Network paths such as
  `\\storage01\Programs` and drive letters such as `Z:\Programs` both work.
* **Edit** (or double-click a path, or press F2) changes the selected path in place.
* **Remove** (or the Delete key) takes the selected path out of the list.

These are used by every department that has no folders of its own (see *Departments → Search in*). Removing a folder here only stops ContentShare searching it; **nothing on the server is
touched**. A folder listed twice (even with different capitals or a trailing backslash) is only
kept once. Press **Save settings** to keep your changes, then refresh the index (see *Index*).

### Reply text placeholders

Write `{requester_name}`, `{folders}` (the copied folders, one per line), `{file_text}` ("3 files"),
`{total_size}`, `{program}`, `{file_count}` and `{date}` where you want those details filled in. Any other
name in braces is refused when you save. `{destination_path}` is still accepted in older texts, but it now
shows the folder names, never a path: the network location is not put in a reply.

### Updates

ContentShare asks GitHub's public releases page whether a newer version exists. Nothing about
your PC, settings or mail is sent. Untick the box to turn the start-up check off. **Check for
updates now** does it right away.

## Departments

Each department is a set of senders, **one folder** its copies go to, and (if you want) its own folders to search. Only emails from these
senders are ever read. Use **Add…**, **Edit…** (or double-click) and **Remove**.

| Field | Meaning |
|---|---|
| **Name** | What the department is called in the app. |
| **Sender emails** | One address per line. Requests from these addresses are read. |
| **Sender domains** | Optional: anyone at this company domain, for example `example.org`. Public mail services such as `gmail.com` or `outlook.com` are refused, because anyone can have an address there; list those people's addresses instead. |
| **Copy into** | The department's folder, for example `\\nas02\media\Incoming`. It must not be inside any folder that is searched (the general storage folders or a department's own), inside Windows or Program Files, or overlap another department's folder. |
| **Search in** | This department's **own folders** to search: a server, a drive or a folder, as many as you like (add them like the storage folders above). Only these are searched for its requests, so each department can have a different server. Leave the list empty and the department uses the **storage folders** from *General*, as before. |
| **If not found, search this whole PC** | Off by default. When nothing is found in the folders above, *Choose content* also looks through this PC's own drives by name. It is slower (a minute or two), starts by itself when nothing was found, can be stopped, and only **suggests** matches: nothing found this way is marked Ready, you choose it. It never looks on network drives, in Windows or Program Files, in recycle bins, or in the department's own copy folders. A department with this on may also pick content from anywhere, as with the next row. |
| **Allow choosing from anywhere** | Off by default. When on, **Browse for a folder… / a file…** in the *Choose content* window accepts a folder or file anywhere on this PC or network, not only inside the storage folders. It is still refused inside Windows or Program Files, for a whole drive, and when it overlaps the department's destination folder. Use it for a department whose content is not kept in one place. |
| **Folder names** | Tick **The emails give exact folder names** for a department whose requests name the folder exactly as it is on the storage server (one name per line, in a list, or in quotes). ContentShare then looks for folders (or, failing that, files) with exactly those names (capital letters do not matter; a date written before the name, such as `Nov 22-28`, is ignored) and nothing is guessed: one folder of that name is **Ready**, several are **Needs review** so you pick one, and a name that is not found says so. Whatever this is set to, a line that is exactly a folder's name is found as that folder. Leave it off for a department whose emails are free-form: you get the closest matches, and you can **Browse for a folder…** yourself. |
| **Subfolder** | How each program's folder is named inside it. Default `{program}`. A single file is copied straight into the department's folder, without a subfolder. You can also use `{date}` and `{department}`. |
| **Reply text** | This department's own reply. Leave empty to use the default. |

## Gmail

| Setting | What it does |
|---|---|
| **Client file** | The `client_secret.json` you downloaded from Google Cloud. See [Connect Gmail](help:gmail). |
| **Connect Gmail… / Disconnect** | Signs in through Google's page, or revokes access and forgets the sign-in. |
| **Label processed emails** | Adds the label **ContentShareAI/Processed** to handled mail. It needs the `gmail.modify` permission. If you change it, disconnect and connect again. |
| **Treat a sender as verified only if Gmail confirms it** | Recommended. Only mail that passed Gmail's sender checks is treated as genuine; the rest needs an extra tick before copying. |
| **Look back** | How many days of mail a check looks through. |
| **Emails per check** | The most emails read in one check. |

## Matching and copying

| Setting | What it does |
|---|---|
| **Mark Ready when at least** | How good a match must score (50–100) before a card says **Ready**. Anything lower is **Needs review**. |
| **…and clearly ahead of the next by** | A match must also beat the runner-up by this many points, so two near-identical programs are never picked automatically. |
| **Verification checksum** | How copies are compared with the originals: `xxh3_128` (fastest, the default), `blake3`, or `sha256`. All are reliable for catching damaged copies. |
| **Keep a checksum file in each copied folder** | Off by default. Every file is already checked against the original while it is copied, whether or not this is on. Switch it on to also leave a `<folder>.checksums` file beside the copy, which anyone can use later to check the files again (`xxhsum -c`, `sha256sum -c`). |
| **Copies at the same time** | 1–4 programs copied together. Use 1 if the network share struggles. |
| **Files at the same time** | 1–8 files of one program copied together (default 4). Many small files copy much faster this way, because each one spends most of its time waiting for the disk or the server. Every file is still checked on its own. Use 1 if the storage server or the destination disk struggles with several at once. |
| **Attempts per file** | How many times a file that fails verification is copied again before the request shows **Failed**. |

Lower the Ready score only after checking real matches in the review list: a wrong automatic
match is worse than a request that waits for you.

## Index

The index lists folder and file names on the storage server so requests can be matched quickly.
**Refresh index now** reads the server again (this can take a few minutes on a large server). It
only reads. ContentShare also refreshes it about once a day, and keeps the previous index if a
scan fails.

[Back to Getting started](help:start)
