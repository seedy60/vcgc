# PlayPalace Web Client

This is an early version of a web client for the PlayPalace. is designed to connect to a server you indicate and can be hosted on the web.

## Public Setup

1. Copy the `/clients/web` directory to a public website location.
2. Copy the sounds folder from `/clients/desktop` to the same place, and put it in `./sounds`.
3. Copy the config sample to config.js and update any of the items if desired, such as server, port, or sounds folder.
4. GO to your new URL. You should see a login screen.

## Local Setup

1. Start the web server:
   - `python3 -m http.server 8080`
3. Open:
   - `http://127.0.0.1:8080/clients/web/`
## How it Works

When viewing the web client from a computer, you can tab between game, history, and chat similar to the desktop client. Most desktop hotkeys work here as well.
From mobile, the menu items become buttons, and you can expand and collapse the history.
Other desktop features like the buffers also work here.

## Theme Verification

The web client follows the operating system light/dark theme automatically.

When verifying theme changes, check:
- the main page and panel surfaces
- dialogs and form controls
- menu selection state
- history and chat readability
- both desktop and narrow/mobile widths

## Session Verification

The web client now retries unexpected disconnects automatically for up to 60 seconds when a saved session or refresh token is available.

When verifying reconnect behavior, check:
- valid saved auth still restores a session on page load
- unexpected disconnect starts reconnect attempts automatically
- manual disconnect does not auto reconnect
- reconnect stops after 60 seconds and returns to the login dialog
- reconnect success restores normal in-game state without reopening the login dialog mid-retry

## Browser Tests

Install the test dependency and Chromium once, then run the Playwright suite:

```bash
cd clients/web
npm install
npx playwright install chromium
npm test
```

The tests serve this directory locally and mock the WebSocket protocol, covering
registration, login-time email verification, password reset, account email settings,
invalid, expired, or locked confirmation codes for reset and email-change flows, and
resending a code after it expires or locks, completing with the newly issued code,
and account settings behavior when email delivery is unavailable.
