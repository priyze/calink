calink

scans your gmail for submission deadlines and puts them in google calendar. setup takes maybe 10 minutes and you only do it once.


files

calink.py - the actual script
cleanup.py - deletes the events the script made
requirements.txt - what pip needs to install
readme.txt - this


setup

1. python
you need python 3.9 or newer. type python --version in a terminal to check.

2. install the packages
open this folder in vs code, open the terminal (ctrl + `) and run these one by one:

   python -m venv venv
   venv\scripts\activate
   pip install -r requirements.txt

on mac or linux the second line is source venv/bin/activate instead.
if powershell complains that scripts are disabled, run set-executionpolicy -scope currentuser remotesigned once and try again.
when it works you'll see (venv) at the start of the line. you have to run the activate line again whenever you open a new terminal.

3. get credentials.json from google
this is the annoying part, but you only do it once. it's what lets the script into your own gmail and calendar.

- go to console.cloud.google.com and make a new project. the name doesn't matter.
- apis & services > library. enable "gmail api" and "google calendar api".
- apis & services > oauth consent screen (might be called google auth platform now). pick external and fill in an app name and your email. then add your own gmail address under test users. don't skip that bit, google blocks your login if you do.
- apis & services > credentials > create credentials > oauth client id. pick desktop app, hit create, download the json.
- rename the file to credentials.json and put it in this folder, next to calink.py.


running it

do a dry run first. it only prints what it found and adds nothing:

   python calink.py --dry-run

the first time it opens a browser so you can log in. google will say the app isn't verified, that's just because you made it yourself. click advanced, go to the app, allow.

if the list looks right, run it for real:

   python calink.py

some extras:

   python calink.py --days 60
   python calink.py --query "from:prof@college.edu"
   python calink.py --all

--days changes how far back it looks (default 30). --query limits it to emails that match a gmail search. --all scans every email instead of just the ones that sound like deadlines.


what to expect

- words like "tomorrow", "in one day" or "friday" are counted from the day the email was sent, not from today, so old emails land on the right date (and get skipped if that date has passed).
- a date with no time becomes an all-day event, with a reminder at 9am the day before.
- a date with a time gets reminders 1 day and 2 hours before.
- if a few emails are about the same thing you get one event, not three.
- submission confirmation emails get skipped.
- dates that have already passed get skipped.
- running it twice is fine, it remembers what it already did. 
- pdfs do NOT get scanned. only email bodies/subjects/titles do.
- deadlines for competitions, hackathons, programs, etc. will also get logged into your calendar.


deleting what it added

   python cleanup.py --dry-run
   python cleanup.py

the first one just lists them. the second asks you to type yes and then deletes them. it only touches events the script made, the rest of your calendar stays as it is.


when stuff breaks

"access blocked" and you can't get past it: you didn't add your email as a test user. see step 3 of setup.

login stopped working after about a week: google expires logins for apps in testing mode after 7 days. delete token.json, run the script again and log in again.

"quota exceeded" or "rate limited": give it a minute, the script waits and retries by itself. a shorter window like --days 14 helps too.

"no module named something": your venv isn't on. run the activate line again, then pip install -r requirements.txt.

"credentials.json not found": it's in the wrong folder or named wrong.

it missed a deadline or added junk: open calink.py and ctrl+f for these near the top.
   deadline_words - words that make a sentence count as a deadline
   confirmation_re - phrases that make an email count as a confirmation
   duplicate_threshold - how similar two deadlines need to be to count as the same one (0 to 1)


don't share these

credentials.json, token.json and seen.db. don't put them on github or send them to anyone. the first two give access to your account and seen.db has bits of your emails in it.

i hope this helps you guys!! :D
if there's any errors, or if you have any suggestions, feel free to text me on discord @pr1yze.