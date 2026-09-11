# Audiobook Factory — user manual

This turns an ebook into an audiobook read aloud in a voice you record yourself.

You do not need to know how any of it works. Everything happens in a normal web
page on your own computer, apart from two short commands to install it and to
open that page. Both are written out below exactly as you should type them.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Setting up, once](#2-setting-up-once)
3. [Opening the control panel](#3-opening-the-control-panel)
4. [Recording your voice](#4-recording-your-voice)
5. [Making the voice usable](#5-making-the-voice-usable)
6. [Adding a book](#6-adding-a-book)
7. [Making the audiobook](#7-making-the-audiobook)
8. [Listening to it](#8-listening-to-it)
9. [Giving characters their own voices](#9-giving-characters-their-own-voices)
10. [Checking it read everything correctly](#10-checking-it-read-everything-correctly)
11. [How long things take](#11-how-long-things-take)
12. [Doing a whole shelf at once](#12-doing-a-whole-shelf-at-once)
13. [When something looks wrong](#13-when-something-looks-wrong)
14. [Words you will see](#14-words-you-will-see)

---

## 1. What you need

- A computer running macOS, Linux or Windows.
- A microphone. The one built into a laptop is fine.
- A quiet room for three minutes.
- An ebook, as an `.epub`, `.pdf` or plain `.txt` file.
- About 5 GB of free disk space.

You do **not** need an internet connection after the first setup, and nothing
you record or read is sent anywhere. It all stays on your computer.

---

## 2. Setting up, once

This happens once per computer and takes a few minutes, most of it waiting.

Open the **Terminal** application. On macOS press `Cmd + Space`, type
`Terminal`, press Enter. On Windows, open **Git Bash**.

Type this and press Enter:

```
cd audiobook-factory
./install.sh
```

On Windows the second line is `./install.ps1` instead.

It will print a list of things it is installing and finish by saying
**all four environments ready**. If it asks for your password, that is the
system asking permission to install a program; type it and press Enter. Nothing
is shown as you type a password, which is normal.

If it stops with an error, see [section 13](#13-when-something-looks-wrong).

---

## 3. Opening the control panel

Everything else happens here. In the Terminal, type:

```
just ui
```

It will print `studio on http://127.0.0.1:8765`. Open a web browser and go to
that address. You can also click it in most Terminals.

Leave the Terminal window open. Closing it closes the control panel. If you
close it by accident, nothing is lost; type `just ui` again.

The page has three areas across the top: the project name, what you are looking
at, and a **library** link on the right.

---

## 4. Recording your voice

Click **library** at the top right.

Under **Add material** you have two ways to give it your voice:

**Record it now.** Click **Record**. Your browser will ask permission to use the
microphone; allow it. A timer appears. Read aloud for two to three minutes, then
click **Stop**. It saves automatically.

**Or upload a file** you recorded elsewhere, on a phone for instance. Click
**Choose file**, pick it, then click **Upload**. Most audio formats work.

### What to read

Open the file `docs/reading-script.txt` in the project folder and read that. It
is written to cover the range of sounds the computer needs to hear.

If you would rather read something else, read for at least two minutes from a
book, in your normal reading-aloud voice. Do not perform. The recording teaches
the computer how you sound when reading, so read the way you would want the
audiobook to sound.

Mistakes cost nothing. If you stumble, say the sentence again and carry on.

Your recording now appears under **Voice samples**.

---

## 5. Making the voice usable

Your recording is now listed under **Voice samples**, with a box beside it.

1. Type a name for the voice into the box. Lowercase letters and no spaces, such
   as `michal` or `narrator`.
2. Choose the language you read in from the dropdown beside it.
3. Click **Create voice**.

This takes a few minutes. The page shows what it is doing as it goes, and you
can stop it with **Cancel**. The first time is slower, because it downloads a
speech model it then keeps.

When it finishes, your voice appears under **Voices** on the front page. Click
it and press play on the clip.

**Listen to it now.** This is the moment to judge whether the voice sounds right,
before spending hours on a whole book.

If it sounds thin, muffled or robotic, the recording is usually the cause rather
than the computer. Record again somewhere quieter, closer to the microphone, or
for longer.

## 6. Adding a book

Back on the **library** page, under **Add material**, next to **Ebook**, click
**Choose file**, pick your book, and click **Upload**.

It appears under **Ebooks** with a box next to it. Type a short name into that
box, lowercase and without spaces, such as `solaris`. This is just a label so
you can find it later. Click **Ingest**.

After a moment the book appears on the front page. Click the project name at the
top left to get there, then click the book.

---

## 7. Making the audiobook

You are now on the book's page. It shows how many chapters were found and
roughly how long the finished audiobook will be.

**First, check the book was read correctly.** In the **Run** panel, click
**Dry run**. This takes seconds and produces a silent version, which sounds
useless but proves the structure is right. Look at the **Chapters** list that
appears. If the chapter titles match your book, everything is fine. If there is
only one chapter and your book has many, see
[section 13](#13-when-something-looks-wrong).

**Then make it for real.** Click **Synthesise**.

A progress bar appears with a running count and an estimate of the time left.
Below it is a stream of text; that is the computer talking to itself and can be
ignored unless something goes wrong.

You can close the browser, close the Terminal, or shut the lid. The work carries
on. Come back later and open the control panel again to see how far it got. If
it is interrupted, click **Synthesise** again and it picks up where it stopped
rather than starting over.

**Finally, click Assemble.** Choose `m4b`, `mp3` or `wav` from the dropdown
first. Use **m4b** for an audiobook app, because it keeps the chapter marks.
Use **mp3** if you want a single file that plays anywhere.

---

## 8. Listening to it

The finished audiobook appears in the **Audiobook** section of the book's page,
with a play button.

Use the download links in the book’s narration history to keep any finished
version. Each narration has its own folder under `data/runs/`, so a new voice
or model does not overwrite a previous audiobook. Older files under `data/out/`
remain available as historical versions. Copy the download to your phone or
music app like any other audio file.

---

## 9. Giving characters their own voices

By default one voice reads everything.

The computer marks each paragraph as narration or as someone speaking, by
looking at quotation marks and dashes. It is right most of the time and
deliberately cautious: when unsure, it leaves a line with the narrator, because
narration read in a character's voice is more jarring than the reverse.

**To use a second voice**, record and set up another voice as in sections 4 and
5. Then on the **library** page, under **Cast**, click **Add role**, type a name
for the character, choose the voice, and click **Save cast**.

The character name must match how the book labels them. If the text says
`Kelvin: — I'm going home`, the role is `kelvin`.

**To correct a single line**, scroll to **Fragments** at the bottom of a book's
page. Every line has a dropdown showing who reads it. Change one and it saves
immediately. Then click **Chunk**, and **Synthesise** again, to hear it.

Corrections are remembered even if you rebuild the book later.

---

## 10. Checking it read everything correctly

The computer occasionally mumbles, skips a few words, or repeats itself. It
never says so, because it does not know.

Click **Verify** on the book's page. It listens back to what it made and
compares that against the text. This takes a while, roughly as long as the
audiobook itself.

When it finishes, click **Review flagged fragments**. Each entry shows what was
written, what it heard, and lets you play the audio.

**Expect some flags even when everything is fine.** The listening step mishears
too. If it wrote "Chapter 2" where the book says "Chapter two", that is not a
mistake in your audiobook. What matters is a line whose ending is missing, or
one that repeats.

Where a line really is wrong, click **Re-render this fragment**. It tries again,
and the result differs slightly each time, so a second attempt is usually clean.
Then click **Assemble** again.

---

## 11. How long things take

On a laptop, roughly:

| Step | Time |
|---|---|
| Recording | 3 minutes |
| Setting up the voice | 3 to 5 minutes |
| Adding a book | seconds |
| Dry run | seconds |
| **Making the audiobook** | **about 2.5 hours per hour of finished audio** |
| Checking it | about as long as the audiobook |

A three-hundred page novel is roughly ten hours of audio, so around a day of
computer time. Start it in the evening. The computer can sleep; the work resumes.

A computer with a dedicated graphics card does this many times faster.

---

## 12. Doing a whole shelf at once

Adding books one at a time is fine for one book. For twenty, open the control
panel and click **batch** at the top right.

**Point it at a folder.** Type the folder's path in the box and press **Scan**.
Scanning changes nothing: it tells you which files are new, which are already
here, and which are copies of each other under different names. When it looks
right, press **Import**.

If every book in the folder is in the same language, choose it beside the box
before importing. A book the program cannot read confidently stops on its own
and the rest carry on, so nothing waits on the one that needs a decision.

**Look at the table before you start.** Each book shows how its file was read,
what language it was taken to be, who will read it, how long it will be, and
what is left to do. A book that needs a decision says so in red and cannot be
ticked; the reason is written underneath it.

**Choose and go.** Tick the books you want, pick a voice for the batch if you
want them all read by one person, and press **Queue selected**. One book can
disagree with the batch: change its voice in its own row.

**Watch it.** The queue at the bottom shows every step. Books are done one step
at a time, and only one thing uses the graphics card at once, so a batch takes
as long as its parts added together. You can **Hold** a step, **Drop** it, or
press **Try again** on one that failed. If a book fails, only that book stops.

## 13. When something looks wrong

**The setup stops with an error.** Read the last few lines; it usually names
what is missing and how to install it. If it mentions `ffmpeg`, `uv` or `just`,
run `./install.sh` again, which installs those.

**`just: command not found`.** The setup did not finish, or this is a new
Terminal window that has not noticed. Close the Terminal, open a new one, and
try again. If it persists, run `./install.sh` again.

**The control panel will not open in the browser.** Check the Terminal where you
typed `just ui` is still open and has not printed an error. The address must be
exactly `http://127.0.0.1:8765`.

**The disk is nearly full.** The program works out how much room a book needs
before it starts, and refuses rather than filling the disk in the middle of the
night. Free some space and try again; nothing already made is lost.

**The cover picture is missing or wrong.** If the book was an `.epub` its own
cover is used. To change it, or to add one to a plain text book, put a picture
named `cover.jpg` or `cover.png` in the book's folder under `data/book/`, then
make the audiobook file again. Nothing else needs redoing.

**One character is quieter than the narrator.** Each voice is measured when it
is made, and the program evens them out. If a voice was made before that
existed, or you re-recorded it, measure it again by typing `just level` and the
voice's name. Anything already read in that voice will need making again,
because it was read at the old volume.

**It says the graphics card is busy.** Only one thing can use it at a time.
Making the audiobook, creating a voice and checking a reading all use it, so
the second one you start is asked to wait. Nothing is lost: wait for the first
to finish, or stop it, then start the other.

**The book has one chapter but should have many.** The chapter headings were not
recognised. `.epub` files usually work best. A plain text file needs its chapter
titles written on their own line starting with `# `, like `# Chapter One`.

**The audiobook is the right length but silent.** You pressed **Dry run**
before **Synthesise**. A dry run is a rehearsal: it makes a file of exactly the
right shape and length out of silence, in seconds, so you can check the
chapters without waiting hours. Pressing **Synthesise** afterwards now throws
that silence away and records for real. Do that, then **Assemble** again.

The book shows **silence** next to its name for as long as its audio is a
rehearsal, and assembling one prints a warning in the Terminal.

**The voice sounds robotic or metallic.** Almost always the recording. Try again
in a quieter room, closer to the microphone, and read for longer. Two minutes is
the minimum; three to five is better.

**A character is read by the narrator.** The character is not in the **Cast**
list, or the book does not label them in a way the computer recognises. Add the
role as in section 9, or fix individual lines with the dropdowns.

**Synthesising seems stuck.** Check the progress bar is still moving. It updates
every few seconds. Some lines take longer than others. If it has not moved in
several minutes, click **Cancel**, then **Synthesise** again; it resumes from
where it stopped.

**Something else.** The Terminal window is where errors appear in full. Copy the
last twenty lines when asking for help.

---

## 14. Words you will see

**Fragment** — one short piece of text, usually a sentence or two. The computer
reads one at a time. A book becomes thousands of them.

**Chunk** — splitting the book into those fragments. It happens automatically.

**Dry run** — a silent rehearsal. It checks the structure in seconds without
doing the slow work. What it produces is silence, and **Synthesise** replaces
it.

**Synthesise** — actually producing the speech. This is the long step.

**Assemble** — joining the pieces into one audiobook file with chapter marks.

**Cast** — which voice reads which character.

**Role** — a character, or `narrator` for the storytelling parts, or `dialogue`
for speech where the book does not say who is talking.

**Audition** — a short sample of your cloned voice, so you can judge it before
committing to a whole book.

**Verify** — listening back to the finished audio and comparing it to the text.

**m4b** — an audiobook file that remembers chapters. **mp3** plays anywhere but
has less reliable chapter support.


## Keeping your library safe

The library now saves books, text and language decisions, voices, narration
settings and progress in a local SQLite database. Original imported files and
voice references are preserved as separate files alongside it.

The Batch page keeps recent import results. If a file needs attention, choose
its correct language or text encoding and import it again. Earlier text versions
remain recorded. Polish and English can have different default models; each
narration remembers the model chosen when it was prepared.

Before updating an existing installation, stop the control panel and run
`just catalog-migrate` once. For a backup, use
`just catalog-backup /path/to/a/new-backup-folder`. Copy that backup to another
drive for protection against disk failure. Detailed recovery instructions are in
[STORAGE-OPERATIONS.md](STORAGE-OPERATIONS.md).
