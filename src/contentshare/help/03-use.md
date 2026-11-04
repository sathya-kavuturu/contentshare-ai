# Using the app

## Requests

Press **Check now** to read new mail. **Each email is one card.** When an email asks for several
folders (a list), the card has a numbered section for each folder, with its own status, the path
that was found and its own buttons. Under them are the buttons for the whole email: **Approve and
copy** copies every folder that is Ready, **Skip all** and **Reject…** apply to all the folders
still open, and **Preview and send reply…** sends **one** reply that names every folder that was
copied and checked (a folder that is not done yet is left out of it). With *Send the reply
automatically* the reply goes out when the last folder being copied is done.

Each folder shows one of:

| You see | It means |
|---|---|
| **✓ Ready** | One clear match was found on the server. |
| **⚠ Needs review** | Several matches, or something differs (date, language, part). Every path found is listed, each with **Use this**; choose the one you want, or press **Search manually…**. |
| **Sender not verified** | Gmail could not confirm who sent it. You must tick one more box before copying. |
| **✖ Failed** | A copy did not verify. The reason is shown, with a **Retry copy** button. |

To copy a Ready card:

1. Check the folder it will copy **from** and **to**.
2. Tick **Confirm copy**.
3. Press **Approve and copy** (in dry-run mode the button says **Check (dry run)**).

**Fewer, shorter cards.** The small arrow at the left of a card (▾) folds it to one short line
showing only the title, the sender's name and the department, plus its status; press it again (▸)
to open it. **Collapse all** at the top of the list folds every request, and the same button then
reads **Expand all**. Folding only changes what you see: ticks you made stay, and new requests
that arrive later follow the last choice. The choice is remembered for next time.

The menu on the left can be folded too: the **☰** icon at its top leaves just the icons (hover
one for its name; the number on the Requests icon is how many are waiting), and **»** brings the
names back. If an update is found while it is folded, it opens so you can see the update box.

**Skip** hides a request (it stays in the History). **Reject…** can send the sender a short
"cannot share" reply, which you can edit first.

A department set to **exact folder names** (Settings → Departments) shows each folder name from the email that was found on the storage server as **Ready**. A name with several folders of that name, or no folder at all, is **Needs review**, and you can browse for the folder yourself.

Lines of the email that no request was made from are listed on the card under **These lines of the email were not searched**, so compare it with the email if something seems missing. To search for one of them (or any other folder) in the same request, press **Add a folder…** on the card: click one of the listed lines or type a name, then choose the folder on the server. It becomes one more section of the same request and the same reply. The button is on every card, also for a request that was already copied: the new folder is copied and checked on its own. **Show text** on the card shows the start of the email.

If a program is not found, press **Search manually…** and type part of its name, or press **Browse for a folder…** / **Browse for a file…** and pick it yourself on the storage server. What you pick must be inside one of the storage folders in *Settings* (it does not have to be in the index), unless the department has **Allow choosing from anywhere** switched on, in which case it can be anywhere. If a whole program is missing from every search result, refresh the index in *Settings → Index*.

## Active copies

Shows progress, speed and time left. **Pause**, **Resume** and **Cancel** work at any time.
When a copy finishes, every file has been read back from the destination and compared with the
original:

* A file that does not match is copied again (up to *Attempts per file*).
* The data is forced onto the disk and then read back *from the disk*, not from Windows' memory, so a copy that only looked complete cannot pass.
* A file is only given its real name after it has passed; until then it has the temporary ending `.cspartial`, which is removed if the copy stops.

To be fast, the next piece of a file is read while the current piece is being written, and several files are copied at once (*Settings → Matching and copying → Files at the same time*). Neither changes the checking: each file is compared on its own, and a file that fails is never put under its real name.
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

Every time ContentShare opens it asks you to **sign in**: choose your name (the last user is already
selected) and type your password, or press **Create a new user…** and type a name and a password twice. Any
password will do. The **Approved by** column shows who approved each request, so a team sharing the app can see
who did what, and the step-by-step record names the same person for every action they took. To sign in as
someone else, click the name button in the menu on the left. **Quit ContentShare** in the box closes the app.
If you forget a password, create a new user. This tells people apart; it is not strong protection. Older
requests show a dash.

The **Copy time** column shows how long the last copy of each request took, from the first file to the last
check (for example `2 min 05 s`). Select a request to also see its average speed. That speed counts the
whole job, which includes reading every file back to check it, so it is lower than a plain copy would show.
If a copy was paused, the time it stood still is included. A request that has not been copied yet, or was
only a dry run, shows nothing. The CSV has the same figure in seconds (`copy_seconds`).

## The automatic check, the tray and pop-ups

* ContentShare reads new mail by itself, once a day at a set time or every 15 minutes to 12 hours
  (*Settings → General → How often*). That only **adds cards**; it never copies or sends. If the
  PC was off when a check was due it checks when it is next on.
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
