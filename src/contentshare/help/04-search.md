# How searching works

ContentShare does two searches for every request: it **searches the mailbox** to find the
request emails, then **searches the storage folders** to find the program that was asked for.
Both only read. Nothing in Gmail is deleted or changed (apart from the optional
**ContentShare/Processed** label), and nothing on the storage server is touched.

## Part 1: Searching the mailbox

### Which emails are looked at

When you press **Check now** (or the daily check runs), ContentShare asks Gmail for mail that
matches **all** of these:

* it is **from** one of the sender addresses or sender domains of your departments
  (*Settings → Departments*),
* it arrived within the last **Look back** days (30 by default),
* it does **not** already carry the **ContentShare/Processed** label (when labels are on),

and reads at most **Emails per check** of them (100 by default). Every other email in the
mailbox is never opened, and a sender that is not in a department is never asked for.

### What happens to each email

1. **Checked again after it is read.** Gmail's search is only a convenience, so each message is
   checked a second time. These are set aside (and remembered, so they are not read again):
   * mail sent by this account,
   * mail from someone who is not in any department,
   * a reply such as *"Thanks, received!"* in a conversation that was already handled, unless it
     asks for something new.
2. **The sender is checked.** Gmail's own result is used (the sender passed *DMARC*, or both
   *SPF* and *DKIM*). A request that cannot be confirmed is never marked Ready: it needs your
   extra tick before anything is copied.
3. **The text is cleaned.** Only the subject and what the sender wrote are used. Quoted replies,
   signatures, "Regards…", links and email addresses are removed. Attachments are never opened,
   links are never followed, and only the first 20,000 characters are read. If an email has no
   plain text, its HTML is reduced to the visible words.
4. **The request is read.** ContentShare looks for the parts below. It uses fixed rules, not
   guesswork, and nothing from an email is ever used as a file path.

| Part | What is recognised |
|---|---|
| **Program name** | Text in quotes, if there is any. Otherwise what is left of the subject (or the first lines) after greetings and request words such as *hi, please, share, send, recording, video, folder* are removed. |
| **Date** | `12 Oct`, `12 October 2025`, `2026-10-12`, `03/04/2026` (day first by default, so 3 April), ranges such as `12 Oct - 14 Oct`, and *yesterday* or *last Sunday*. A date without a year means the most recent such date. |
| **Language** | A language name, or a short code such as `EN` or `HI`. Short codes count only in capitals, so a greeting "Hi" is not read as Hindi. |
| **Part** | *part, episode, day, session, chapter, volume, class* or *module* followed by a number. |
| **Format** | *video, audio, subtitles, photos, documents*, or an extension such as `mp4` or `srt`. |

A list in the email (lines starting with `-`, `*`, `•` or `1.`) gives **one request per item**,
and each item uses the date, language or part given in the email as a whole unless it states its
own. Two different dates give one request per date, and the same day written twice (subject and
body) counts once. At most 20 requests are made from one email.

## Part 2: Searching folders and files

### The index

Searching a network server directly is slow, so ContentShare keeps a **local list of the folder
and file names** on the storage server (with sizes and dates, never what is inside the files).
This is the *index*. It is built by **Refresh index now** (*Settings → Index*) and refreshed about
once a day.

* The scan skips recycle bins, `System Volume Information`, `Thumbs.db`, `desktop.ini`,
  `~$` temporary files and shortcuts or links.
* If a scan fails, the previous index is kept.
* A folder added to the server after the last refresh is **not found by search** until the index
  is refreshed. Browsing for it yourself always works, because that looks at the server directly.

### Finding the program: the default way (closest matches)

This is used for departments whose emails are written freely.

1. **Gather candidates.** The words of the program name (letters and digits only, up to 8) are
   looked up in the index, so `SunriseTalk` is searched as *sunrise* and *talk*. Names that start
   with one of the words count, and items whose name or parent folders contain the requested date
   (in any spelling, such as `2026-10-12` or `20261012`) come first. If very few are found, names
   that merely share a few letters are added, so a spelling mistake does not hide the program.
2. **Score each one from 0 to 100.** The score starts with how much the **name** looks like the
   request. The date, language, part and format are taken out of the name first and compared
   separately. A name matched through a parent folder counts slightly less.

   | Compared | If it agrees | If it differs |
   |---|---|---|
   | **Date** | +10 | −30 (−5 if the folder shows no date) |
   | **Language** | +6 | −25 |
   | **Part** | +8 | −25 |
   | **Format** (files only) | +3 | −10 |

   Only items scoring 40 or more are kept, the best five are shown, and a folder is never shown
   together with the files inside it.
3. **Decide Ready or Needs review.** A request is **✓ Ready** only if **all** of this is true:
   * the best match does not differ in date, language, part or format,
   * its **name alone** scores at least **Mark Ready when at least** (85 by default), so a
     matching date can never rescue a vague name,
   * it is ahead of the second best by at least **…and clearly ahead of the next by** points
     (5 by default),
   * the sender was verified, and the destination folder is allowed.

   In any other case the card says **⚠ Needs review** and gives the reason. The best matches are
   listed on the card so you can pick one with **Use this**.

### Finding the program: exact folder names

A department with **The emails give exact folder names** ticked (*Settings → Departments*) is
searched differently, and nothing is guessed.

* ContentShare takes the names from the email: one per line, in a list, in quotes, or a single
  word that looks like a folder name, such as `2026-10-12_SunriseTalk_EN` (it has an underscore or
  a number and is not just a date). Up to 60 names are looked up.
* Each name is looked up in the index as a **folder with exactly that name**. Capital letters do
  not matter, as in Windows.
* **One** folder with that name: **Ready**. **Several** folders with that name: **Needs review**,
  and you pick one. **None**: **Needs review**, and the card says no folder with that name was
  found.
* If the email names a folder and also a folder inside it, the inner one is meant.

### Choosing it yourself

On any card, **Change content** opens *Choose content*. There you can **search by name** (the
same index search as above), or press **Browse for a folder…** / **Browse for a file…** and
pick it in the usual Windows dialog. A pick must be inside one of the storage folders, unless the
department has **Allow choosing from anywhere** ticked. The pick does not have to be in the index.
Your choice always counts as the right one, and it is checked again when you approve and just
before the copy starts.

### After a match

Before a copy starts, the folder is measured again on the server, so a match that has since
moved or changed is caught. The program is copied into the department's folder, in a subfolder
named from the department's **Subfolder** pattern (by default the program's own name).

## Why a request says "Needs review"

The card always says why. These are the usual reasons:

| What the card says | What it means | What to do |
|---|---|---|
| **Could not tell which program was requested** | No program name could be found in the email. | Press **Change content** and choose it. |
| **No content found for "…"** | Nothing in the index looks like the name. | Refresh the index, or browse for it. |
| **Closest match differs from the request** | The best match has another date, language, part or format. | Check the note under it; use it only if it is right. |
| **Best match is only …% sure** | The name is too different to trust. | Pick the right one from the list, or search by name. |
| **… similar items found; please pick the right one** | Two or more are almost equally good. | Pick one. |
| **Sender could not be verified** | Gmail could not confirm who sent it. | Check the request carefully, and tick the extra box if it is genuine. |
| **… folders are named "…"** | (exact folder names) Several folders have that name. | Pick one. |
| **No folder named "…" was found** | (exact folder names) No folder has that name. | Refresh the index, or browse for it. |

[Using the app](help:use) · [Settings explained](help:settings) · [Troubleshooting](help:trouble)
