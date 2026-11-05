# Smart reading

Normally ContentShare reads only mail from the senders you listed in a department. **Smart reading**
also looks at mail from people who are in no department, finds out whether it asks for content, and
pulls out the folder names, using a small language model that runs on this PC.

## What it does with an email

1. **Fetch.** Recent mail in your main inbox. Mail you sent and mail from automatic senders (no-reply
   and similar) is left out.
2. **Quick check, no model.** If lines of the email are exactly the names of folders on the storage
   server, that is already a request.
3. **The model.** Otherwise, if the email contains words that ask for something, the model reads it and
   answers: "is this a content request, and which folder names does it give?" Only names that are
   really written in the email are kept. Anything else (a newsletter, a question, an invoice) is
   remembered as "not a request" and is not read again.
4. **Look up.** Each name is searched on the storage server.
5. **You decide.** These requests always wait for you. Because the sender is in no department, press
   **Choose department…** on the request, check the names (a note says they were read by the smart
   reader), choose the folders, then approve as usual. Nothing is copied or sent by itself.

## Setting it up

Open **Settings → Smart reading**, turn it on, press **Save settings**, then **Download…**. The model
and the program that runs it (about 2.5 GB with the recommended model) are downloaded once. Each
file is checked against a known checksum before it is used; a file that does not match is refused.
**Try it on an email…** shows what the model finds in an email you paste. Nothing is saved or sent.

## What you need

* A Windows PC with 8 GB of memory or more. The model uses about 3 GB while it reads and is stopped
  again after a few idle minutes (you can change this).
* A graphics card with 2 GB or more makes it faster: tick **Use the graphics card**. Without one, an
  email takes some seconds to a little over a minute to read, which is fine for a check every
  15 minutes.
* About 3 GB of free disk space.

## Safety

* **Nothing leaves this PC.** The model runs on your computer. Email text is never sent to an AI
  service.
* **Emails from strangers are untrusted.** An email cannot give the model orders: it only answers in a
  fixed shape, and what it answers is used only as search text, never as a path, command or
  address.
* **Every request waits for you.** There is no automatic copying and no automatic reply for these.
* The first request from a new sender is shown as **Sender NOT verified** whenever Gmail cannot
  confirm who sent it.

## Learning

When **Learn from my approvals and rejections** is on, what you decide is kept on this PC: names you
approved are remembered as right, and requests you skipped or rejected as "not a request". When
the model reads a similar email, it is shown a few of these as examples. This changes its answers
straight away and does not train it. **Forget what it learned** deletes them.

## Gmail use

Reading more mail uses the Gmail API more, but not by much: a check every 15 minutes is 96 checks a
day. Google states that standard use of the Gmail API has no charge, with limits far above this.
See [Settings explained](help:settings).

## The files and their licences

* **Qwen3 4B** and **Qwen3 1.7B** (Alibaba Cloud's Qwen team): Apache License 2.0.
* **llama.cpp** (runs the model): MIT licence.

Both allow use inside an organisation. Models with research-only or community licences are not
used.

[Using the app](help:use) · [Settings explained](help:settings) · [Troubleshooting](help:trouble)
