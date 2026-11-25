# Add-On: push big Odoo modules to GitHub

A small local app for Odoo.sh teams whose modules are too big for GitHub's drag and drop upload.
Pick a folder or drop a zip, choose a repository and branch, and push. No Windows middleman needed.

## Features
- Sign in with GitHub tokens. Your username is detected, and you can save several tokens.
- Searchable repository list and a branch dropdown, with new-branch support.
- Scans your folder, counts Odoo modules, and warns about files over GitHub's 100 MB limit.
- Clones first, so there are no README merge conflicts.
- Live progress for each step, plus a copyable log.

## Install (Ubuntu)
```bash
sudo apt install -y git rsync zenity python3
git clone https://github.com/araza2003/Add-On.git
cd Add-On
python3 odoo-pusher.py
```
It opens in your browser at http://127.0.0.1:8765. Run `python3 odoo-pusher.py --install` to add it to your app menu.

## Tokens
Create a classic token with the `repo` scope (or a fine-grained token with Contents: read and write).
Tokens are stored in `~/.config/odoo-pusher/profiles.json`, readable only by you. The app listens on your own machine only.

## Rebrand
Edit the `BRAND` block at the top of `odoo-pusher.py`.

## Author
Built by [Ahmed Raza](https://www.linkedin.com/in/ahmed-raza-193686146), Python and Odoo developer.
Licensed under the MIT License.
