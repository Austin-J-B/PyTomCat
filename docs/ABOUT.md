# TomCatBot

**TomCatBot** is the Discord bot operated by **Campus Cat Coalition**, a
registered nonprofit student organization at the University of Texas at
Arlington that cares for the cats living on campus.

The bot runs inside the club's private Discord server. It is not a public
product, is not sold, and is not available to anyone outside the club.

---

## What TomCatBot does

- **Membership and dues.** Members join through a form and pay dues through
  PayPal, Venmo, or Cash App. TomCatBot matches each payment to the member who
  made it, marks their membership verified, and assigns their Discord role.
- **Club finances.** It records income and expenses in the club's bookkeeping
  spreadsheet so officers do not have to reconcile every payment by hand.
- **Feeding schedules.** Volunteers sign up for feeding shifts at the campus
  feeding stations, request substitutes, and check off completed rounds.
- **Cat records.** It maintains a catalogue of the individual cats on campus,
  including photos, and uses an image model to help identify which cat appears
  in a submitted photo.

---

## Why TomCatBot requests access to Google

TomCatBot signs in to **one Google account: the club's own mailbox**. It never
requests access to, connects to, or reads the Google account of any member,
visitor, or other person.

**Scope requested:** `https://www.googleapis.com/auth/gmail.readonly` —
read-only. The bot cannot send, modify, archive, or delete mail.

When a member pays dues, PayPal, Venmo, or Cash App sends a payment
notification to the club mailbox. TomCatBot reads those notifications to learn
who paid, how much, and when, so it can verify that member's dues automatically.
That is the only reason the Gmail scope is requested.

TomCatBot also uses **Google Sheets**, through a separate service account, to
store the club's membership roster and financial records.

TomCatBot's use and transfer of information received from Google APIs adheres to
the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

---

## Who runs it

Campus Cat Coalition, University of Texas at Arlington.

- **Privacy policy:** [https://ui.catsofuta.org/privacy](https://ui.catsofuta.org/privacy)
- **Club website:** [https://www.catsofuta.org/](https://www.catsofuta.org/)
- **Contact:** utacampuscats@gmail.com

The bot is open source. Its code, including everything described above, is at
[github.com/Austin-J-B/PyTomCat](https://github.com/Austin-J-B/PyTomCat).
