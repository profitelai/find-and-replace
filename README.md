# Profitell Find & Replace

Search a whole folder for some text, preview every match, replace it, and go **back and forward** through every change you made, with a before/after view of each one. It runs on your own computer; nothing is uploaded anywhere.

## Start it

```bash
cd ~/bizlocal/profitell-replace && ./run.sh
```

Your browser opens at http://localhost:5731. (Python 3.9+ only; nothing to install.)

## How to use it

1. **Folder.** Click *Choose folder…* (a normal Mac folder picker), or type a path, or use *Browse*.
2. **Find and replace.** Type what to find and what to replace it with. Leave "Replace with" empty to delete the found text. Options: match case, whole word, and *Pattern (regex)* for advanced searches (`$1` or `\1` reuses a captured part). You can limit to certain files (`*.html, *.md`) or skip some (`*.min.js`).
   **Exclusions:** *Never change inside these texts* protects words you list (for example `JetBrains`): any match that touches one is left alone. *Skip lines that contain* leaves whole lines alone. The preview tells you how many matches your exclusions skipped, and each version remembers which exclusions it used.
3. **Search** shows a preview only, nothing is changed. Every match is shown with the old line in red and the new line in green. Untick any match or whole file you want to leave alone.
4. **Replace selected.** A restore point is saved first, then the files are changed.
5. **Versions** (right side). Each replace is a numbered version. Click one to see exactly what changed. *Back* and *Forward* move through them, and you can jump to any version, including the original.

## Safety built in

- Searching never writes anything.
- Before any change, the exact old and new content of every touched file is stored (`~/.profitell-replace/`), so *Back* restores them byte for byte.
- If a file changed after you searched, it is skipped, not overwritten.
- Going Back or Forward refuses to overwrite a file you edited by hand outside the tool, unless you confirm.
- If a write fails half-way, everything already written is rolled back.
- Files are written atomically and keep their permissions; line endings (Windows or Mac) and accents are preserved.
- Dot-files and dot-folders (`.env`, `.env.local`, `.htaccess`, `.github/`, `.config/`) are searched like any other file.
- Skipped automatically: binary files, files that are not UTF-8 text, files over 5 MB, and `.git`, `node_modules`, `venv`, `dist`, `build` and similar folders.
- It refuses to run on `/` or your home folder; pick a project folder.
- The server listens on this computer only, and every request needs a one-time token that only the page you opened has.

## Good to know

- History is per folder and linear. If you go Back and then make a new replace, the versions that were ahead are discarded (their saved copies stay on disk).
- Only file contents are changed. File and folder names are not renamed.
- Version history lives outside your folder, so it will not show up in Git or get uploaded with your site. Delete `~/.profitell-replace/` to clear it.

## Tests

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests
```

## Status, safety and license

This tool rewrites files on your disk. Every apply is saved as a version (in `~/.profitell-replace`, or the folder in `PFR_HOME`) so you can go back, but keep your own backups of anything important and preview before you apply. It is provided as is, without warranty, and was drafted by an AI assistant at the direction of Profitell Security Consulting.

Released under the MIT License (see `LICENSE`). Copyright 2026 Profitell Security Consulting. https://www.profitell.com/
