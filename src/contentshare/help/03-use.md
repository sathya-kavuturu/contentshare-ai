# Using the app

## Requests

Press **Check now** to read new mail. Each request line becomes a card:

| You see | It means |
|---|---|
| **✓ Ready** | One clear match was found on the server. |
| **⚠ Needs review** | Several matches, or something differs (date, language, part). Choose one with **Use this**, or **Search manually…**. |
| **Sender not verified** | Gmail could not confirm who sent it. You must tick one more box before copying. |
| **✖ Failed** | A copy did not verify. The reason is shown, with a **Retry copy** button. |

To copy a Ready card:

1. Check the folder it will copy **from** and **to**.
2. Tick **Confirm copy**.
3. Press **Approve and copy** (in dry-run mode the button says **Check (dry run)**).

**Skip** hides a request (it stays in the History). **Reject…** can send the sender a short
"cannot share" reply, which you can edit first.

A department set to **exact folder names** (Settings → Departments) shows each folder name from the email that was found on the storage server as **Ready**. A name with several folders of that name, or no folder at all, is **Needs review**, and you can browse for the folder yourself.

If a program is not found, press **Search manually…** and type part of its name, or press **Browse for a folder…** / **Browse for a file…** and pick it yourself on the storage server. What you pick must be inside one of the storage folders in *Settings* (it does not have to be in the index), unless the department has **Allow choosing from anywhere** switched on, in which case it can be anywhere. If a whole program is missing from every search result, refresh the index in *Settings → Index*.

## Active copies

Shows progress, speed and time left. **Pause**, **Resume** and **Cancel** work at any time.
When a copy finishes, every file has been read back from the destination and compared with the
original:

* A file that does not match is copied again (up to *Attempts per file*).
* If it still fails the request shows **✖ Failed**, and no "shared" reply can be sent.

If you quit, or the PC restarts, in the middle of a copy it **continues from where it stopped**
the next time ContentShare starts.

## Replying

After **✓✓ Verified** the reply is ready. **Send reply…** shows the message and lets you edit
it first. If **Send the reply automatically** was ticked on the card, it is sent as soon as the
copy is verified. Replies go only to the person who asked, in their own email thread. In
dry-run mode nothing is sent.

## History

Every request with a step-by-step record of what happened. **Export CSV** saves it for Excel.

## The daily check, the tray and pop-ups

* At the time set in *Settings → General* ContentShare reads new mail by itself. That only
  **adds cards**; it never copies or sends. If the PC was off at that time it checks when it is
  next on.
* Closing the window with ✕ leaves ContentShare running next to the clock (the **tray**). An
  amber dot on its icon means requests are waiting. **Right-click the icon → Quit** to really
  close it.
* Pop-ups only say things like "3 new requests" or "Request #7: copy verified".

## Updates

When ContentShare starts it checks whether a newer version exists. If so, a box appears at the
**bottom left** of the window:

1. **Download** fetches the installer and checks it against the checksum published with it.
2. When it says *ready to install*, press **Update** to run the installer, or **Cancel** to
   leave it for later.

Nothing is installed without your click, and the update waits while copies are running. Your
settings and history are kept. Turn the start-up check off, or check right now, in
*Settings → General*.

How requests are found, step by step: [How searching works](help:search).

[Settings explained](help:settings)
