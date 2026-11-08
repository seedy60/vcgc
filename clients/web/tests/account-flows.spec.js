const { test, expect } = require("@playwright/test");

const SERVER_URL = "ws://127.0.0.1:8000";

async function mockServer(page, onPacket) {
  await page.routeWebSocket(SERVER_URL, (ws) => {
    ws.onMessage((message) => {
      const packet = JSON.parse(String(message));
      onPacket(packet, ws);
    });
  });
}

function send(ws, packet) {
  ws.send(JSON.stringify(packet));
}

async function waitForLogin(page) {
  await expect(page.locator("#login-dialog")).toBeVisible();
}

test("registration sends a verification code and completes email verification", async ({
  page,
}) => {
  const received = [];
  await mockServer(page, (packet, ws) => {
    received.push(packet);
    if (packet.type === "register") {
      expect(packet.username).toBe("newuser");
      expect(packet.email).toBe("new@example.com");
      send(ws, {
        type: "registration_pending",
        username: packet.username,
        email: packet.email,
        message: "Account created. Enter the verification code sent to new@example.com.",
        email_sent: true,
        expires_in: 900,
      });
    } else if (packet.type === "verify_email") {
      expect(packet.code).toBe("CODE123");
      send(ws, {
        type: "email_verified",
        username: packet.username,
        approved: false,
        message: "Email verified. You can now log in.",
      });
    }
  });

  await page.goto("/");
  await waitForLogin(page);
  await page.locator("#create-account-btn").click();

  const dialog = page.locator("#account-flow-dialog");
  await expect(dialog).toBeVisible();
  await dialog.locator("#flow-username").fill("newuser");
  await dialog.locator("#flow-email").fill("new@example.com");
  await dialog.locator("#flow-password").fill("secret123");
  await dialog.locator("#account-flow-submit").click();

  await expect(dialog.locator("#account-flow-status")).toContainText(
    "Account created"
  );
  await expect(dialog.locator("#flow-code")).toBeVisible();
  await dialog.locator("#flow-code").fill("CODE123");
  await dialog.locator("#account-flow-submit").click();

  await expect(dialog).not.toBeVisible();
  await expect(page.locator("#status")).toContainText("Email verified");
  expect(received.map((packet) => packet.type)).toEqual([
    "register",
    "verify_email",
  ]);
});

test("email verification opens from a login challenge and completes", async ({
  page,
}) => {
  const received = [];
  await mockServer(page, (packet, ws) => {
    received.push(packet);
    if (packet.type === "authorize") {
      send(ws, {
        type: "email_verification_required",
        username: packet.username,
        message: "Verify your email address before logging in.",
      });
    } else if (packet.type === "verify_email") {
      expect(packet.code).toBe("VERIFY42");
      send(ws, {
        type: "email_verified",
        username: packet.username,
        approved: true,
        message: "Email verified. You can now log in.",
      });
    }
  });

  await page.goto("/");
  await waitForLogin(page);
  await page.locator("#username").fill("alice");
  await page.locator("#password").fill("secret123");
  await page.locator("#connect-btn").click();

  const dialog = page.locator("#account-flow-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator("#flow-username")).toHaveValue("alice");
  await dialog.locator("#flow-code").fill("VERIFY42");
  await dialog.locator("#account-flow-submit").click();

  await expect(dialog).not.toBeVisible();
  await expect(page.locator("#status")).toContainText("Email verified");
  expect(received.map((packet) => packet.type)).toEqual([
    "authorize",
    "verify_email",
  ]);
});

test("password reset requests and confirms a new password", async ({ page }) => {
  const received = [];
  await mockServer(page, (packet, ws) => {
    received.push(packet);
    if (packet.type === "request_password_reset") {
      expect(packet.username).toBe("alice");
      send(ws, {
        type: "password_reset_requested",
        message: "If that account has an email address, a reset code has been sent.",
      });
    } else if (packet.type === "reset_password") {
      expect(packet.code).toBe("RESET99");
      expect(packet.new_password).toBe("brand-new-password");
      send(ws, {
        type: "password_reset_complete",
        message: "Password reset. You can now log in with your new password.",
      });
    }
  });

  await page.goto("/");
  await waitForLogin(page);
  await page.locator("#forgot-password-btn").click();

  const dialog = page.locator("#account-flow-dialog");
  await expect(dialog).toBeVisible();
  await dialog.locator("#flow-username").fill("alice");
  await dialog.locator("#account-flow-submit").click();

  await expect(dialog.locator("#account-flow-status")).toContainText(
    "reset code"
  );
  await dialog.locator("#flow-code").fill("RESET99");
  await dialog.locator("#flow-new-password").fill("brand-new-password");
  await dialog.locator("#account-flow-submit").click();

  await expect(dialog).not.toBeVisible();
  await expect(page.locator("#status")).toContainText("Password reset");
  await expect(page.locator("#username")).toHaveValue("alice");
  await expect(page.locator("#password")).toHaveValue("brand-new-password");
  expect(received.map((packet) => packet.type)).toEqual([
    "request_password_reset",
    "reset_password",
  ]);
});

test("account settings sends and confirms an email change", async ({ page }) => {
  let sendOpenSettings = null;
  await mockServer(page, (packet, ws) => {
    if (packet.type === "authorize") {
      send(ws, {
        type: "authorize_success",
        username: packet.username,
        version: "11.0.0",
        session_token: "session-token",
        session_expires_at: Math.floor(Date.now() / 1000) + 3600,
        refresh_token: "refresh-token",
        refresh_expires_at: Math.floor(Date.now() / 1000) + 86400,
      });
      sendOpenSettings = () =>
        send(ws, {
          type: "open_account_settings",
          username: packet.username,
          email: "old@example.com",
          email_verified: true,
          available: true,
        });
    } else if (packet.type === "request_email_change") {
      expect(packet.email).toBe("new@example.com");
      send(ws, {
        type: "email_change_pending",
        email: packet.email,
        message: "A confirmation code was sent to new@example.com.",
        expires_in: 900,
      });
    } else if (packet.type === "verify_email_change") {
      expect(packet.code).toBe("CHANGE7");
      send(ws, {
        type: "email_change_complete",
        email: "new@example.com",
        message: "Email address updated.",
      });
    }
  });

  await page.goto("/");
  await waitForLogin(page);
  await page.locator("#username").fill("alice");
  await page.locator("#password").fill("secret123");
  await page.locator("#connect-btn").click();
  await expect(page.locator("#game-shell")).toBeVisible();

  sendOpenSettings();
  const dialog = page.locator("#account-settings-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator("#account-settings-current")).toContainText(
    "old@example.com"
  );
  await dialog.locator("#account-settings-email").fill("new@example.com");
  await dialog.locator("#account-settings-send").click();
  await expect(dialog.locator("#account-settings-status")).toContainText(
    "confirmation code"
  );
  await dialog.locator("#account-settings-code").fill("CHANGE7");
  await dialog.locator("#account-settings-verify").click();

  await expect(dialog.locator("#account-settings-status")).toContainText(
    "Email address updated"
  );
  await expect(dialog.locator("#account-settings-current")).toContainText(
    "new@example.com"
  );
});

const PASSWORD_RESET_ERRORS = [
  {
    state: "invalid",
    message: "That reset code is invalid or expired.",
  },
  {
    state: "expired",
    message: "That reset code has expired. Request a new one.",
  },
  {
    state: "locked",
    message: "Too many incorrect attempts. Request a new reset code.",
  },
];

const EMAIL_CHANGE_ERRORS = [
  {
    state: "invalid",
    message: "That confirmation code is invalid or expired.",
  },
  {
    state: "expired",
    message: "That confirmation code has expired. Request a new one.",
  },
  {
    state: "locked",
    message: "Too many incorrect attempts. Request a new confirmation code.",
  },
];

async function openPasswordResetDialog(page) {
  await page.goto("/");
  await waitForLogin(page);
  await page.locator("#forgot-password-btn").click();

  const dialog = page.locator("#account-flow-dialog");
  await expect(dialog).toBeVisible();
  await dialog.locator("#flow-username").fill("alice");
  await dialog.locator("#account-flow-submit").click();
  await expect(dialog.locator("#account-flow-status")).toContainText(
    "reset code"
  );
  return dialog;
}

for (const { state, message } of PASSWORD_RESET_ERRORS) {
  test(`password reset keeps the flow open for a ${state} code`, async ({ page }) => {
    const received = [];
    await mockServer(page, (packet, ws) => {
      received.push(packet);
      if (packet.type === "request_password_reset") {
        send(ws, {
          type: "password_reset_requested",
          message: "If that account has an email address, a reset code has been sent.",
        });
      } else if (packet.type === "reset_password") {
        expect(packet.code).toBe("BADCODE");
        send(ws, {
          type: "password_reset_failed",
          message,
          attempts_remaining: state === "locked" ? 0 : 4,
        });
      }
    });

    const dialog = await openPasswordResetDialog(page);
    await dialog.locator("#flow-code").fill("BADCODE");
    await dialog.locator("#flow-new-password").fill("brand-new-password");
    await dialog.locator("#account-flow-submit").click();

    await expect(dialog.locator("#account-flow-status")).toHaveText(message);
    await expect(dialog).toBeVisible();
    await expect(dialog.locator("#account-flow-submit")).toBeEnabled();
    expect(received.map((packet) => packet.type)).toEqual([
      "request_password_reset",
      "reset_password",
    ]);
  });
}

async function connectAndOpenAccountSettings(
  page,
  handlePacket,
  { available = true } = {}
) {
  let openSettings = null;
  const received = [];
  await mockServer(page, (packet, ws) => {
    received.push(packet);
    if (packet.type === "authorize") {
      send(ws, {
        type: "authorize_success",
        username: packet.username,
        version: "11.0.0",
        session_token: "session-token",
        session_expires_at: Math.floor(Date.now() / 1000) + 3600,
        refresh_token: "refresh-token",
        refresh_expires_at: Math.floor(Date.now() / 1000) + 86400,
      });
      openSettings = () =>
        send(ws, {
          type: "open_account_settings",
          username: packet.username,
          email: "old@example.com",
          email_verified: true,
          available,
        });
      return;
    }
    handlePacket(packet, ws);
  });

  await page.goto("/");
  await waitForLogin(page);
  await page.locator("#username").fill("alice");
  await page.locator("#password").fill("secret123");
  await page.locator("#connect-btn").click();
  await expect(page.locator("#game-shell")).toBeVisible();

  openSettings();
  const dialog = page.locator("#account-settings-dialog");
  await expect(dialog).toBeVisible();
  return { dialog, received };
}

for (const { state, message } of EMAIL_CHANGE_ERRORS) {
  test(`email change keeps the flow open for a ${state} code`, async ({ page }) => {
    const { dialog, received } = await connectAndOpenAccountSettings(
      page,
      (packet, ws) => {
        if (packet.type === "request_email_change") {
          send(ws, {
            type: "email_change_pending",
            email: packet.email,
            message: "A confirmation code was sent to new@example.com.",
            expires_in: 900,
          });
        } else if (packet.type === "verify_email_change") {
          expect(packet.code).toBe("BADCODE");
          send(ws, {
            type: "email_change_failed",
            message,
            attempts_remaining: state === "locked" ? 0 : 4,
          });
        }
      }
    );

    await dialog.locator("#account-settings-email").fill("new@example.com");
    await dialog.locator("#account-settings-send").click();
    await expect(dialog.locator("#account-settings-status")).toContainText(
      "confirmation code"
    );
    await dialog.locator("#account-settings-code").fill("BADCODE");
    await dialog.locator("#account-settings-verify").click();

    await expect(dialog.locator("#account-settings-status")).toHaveText(message);
    await expect(dialog).toBeVisible();
    await expect(dialog.locator("#account-settings-current")).toContainText(
      "old@example.com"
    );
    expect(received.map((packet) => packet.type)).toEqual([
      "authorize",
      "request_email_change",
      "verify_email_change",
    ]);
  });
}

for (const { state, message } of PASSWORD_RESET_ERRORS.filter(
  (entry) => entry.state !== "invalid"
)) {
  test(`password reset can resend after ${state === "expired" ? "an" : "a"} ${state} code`, async ({
    page,
  }) => {
    let resetRequests = 0;
    await mockServer(page, (packet, ws) => {
      if (packet.type === "request_password_reset") {
        resetRequests += 1;
        send(ws, {
          type: "password_reset_requested",
          message:
            resetRequests === 1
              ? "If that account has an email address, a reset code has been sent."
              : "A new reset code has been sent.",
        });
      } else if (packet.type === "reset_password") {
        send(ws, {
          type: "password_reset_failed",
          message,
          attempts_remaining: state === "locked" ? 0 : 4,
        });
      }
    });

    const dialog = await openPasswordResetDialog(page);
    await dialog.locator("#flow-code").fill("BADCODE");
    await dialog.locator("#flow-new-password").fill("brand-new-password");
    await dialog.locator("#account-flow-submit").click();
    await expect(dialog.locator("#account-flow-status")).toHaveText(message);

    await dialog.locator("#account-flow-send").click();

    await expect(dialog.locator("#account-flow-status")).toHaveText(
      "A new reset code has been sent."
    );
    expect(resetRequests).toBe(2);
    await expect(dialog).toBeVisible();
    await expect(dialog.locator("#flow-code")).toBeVisible();
  });
}

for (const { state, message } of EMAIL_CHANGE_ERRORS.filter(
  (entry) => entry.state !== "invalid"
)) {
  test(`email change can resend after ${state === "expired" ? "an" : "a"} ${state} code`, async ({
    page,
  }) => {
    let changeRequests = 0;
    const { dialog, received } = await connectAndOpenAccountSettings(
      page,
      (packet, ws) => {
        if (packet.type === "request_email_change") {
          changeRequests += 1;
          send(ws, {
            type: "email_change_pending",
            email: packet.email,
            message:
              changeRequests === 1
                ? "A confirmation code was sent to new@example.com."
                : "A new confirmation code was sent to new@example.com.",
            expires_in: 900,
          });
        } else if (packet.type === "verify_email_change") {
          send(ws, {
            type: "email_change_failed",
            message,
            attempts_remaining: state === "locked" ? 0 : 4,
          });
        }
      }
    );

    await dialog.locator("#account-settings-email").fill("new@example.com");
    await dialog.locator("#account-settings-send").click();
    await expect(dialog.locator("#account-settings-status")).toContainText(
      "confirmation code"
    );
    await dialog.locator("#account-settings-code").fill("BADCODE");
    await dialog.locator("#account-settings-verify").click();
    await expect(dialog.locator("#account-settings-status")).toHaveText(message);

    await dialog.locator("#account-settings-send").click();

    await expect(dialog.locator("#account-settings-status")).toHaveText(
      "A new confirmation code was sent to new@example.com."
    );
    expect(changeRequests).toBe(2);
    await expect(dialog).toBeVisible();
    await expect(dialog.locator("#account-settings-current")).toContainText(
      "old@example.com"
    );
    expect(received.map((packet) => packet.type)).toEqual([
      "authorize",
      "request_email_change",
      "verify_email_change",
      "request_email_change",
    ]);
  });
}

for (const { state, message } of PASSWORD_RESET_ERRORS.filter(
  (entry) => entry.state !== "invalid"
)) {
  test(`password reset completes with a resent code after ${state === "expired" ? "an" : "a"} ${state} code`, async ({
    page,
  }) => {
    const received = [];
    let resetRequests = 0;
    await mockServer(page, (packet, ws) => {
      received.push(packet);
      if (packet.type === "request_password_reset") {
        resetRequests += 1;
        send(ws, {
          type: "password_reset_requested",
          message:
            resetRequests === 1
              ? "If that account has an email address, a reset code has been sent."
              : "A new reset code has been sent.",
        });
      } else if (packet.type === "reset_password") {
        if (resetRequests === 1) {
          expect(packet.code).toBe("OLDCODE");
          send(ws, {
            type: "password_reset_failed",
            message,
            attempts_remaining: state === "locked" ? 0 : 4,
          });
        } else {
          expect(packet.code).toBe("NEWCODE");
          expect(packet.new_password).toBe("brand-new-password");
          send(ws, {
            type: "password_reset_complete",
            message: "Password reset. You can now log in with your new password.",
          });
        }
      }
    });

    const dialog = await openPasswordResetDialog(page);
    await dialog.locator("#flow-code").fill("OLDCODE");
    await dialog.locator("#flow-new-password").fill("brand-new-password");
    await dialog.locator("#account-flow-submit").click();
    await expect(dialog.locator("#account-flow-status")).toHaveText(message);

    await dialog.locator("#account-flow-send").click();
    await expect(dialog.locator("#account-flow-status")).toHaveText(
      "A new reset code has been sent."
    );

    await dialog.locator("#flow-code").fill("NEWCODE");
    await dialog.locator("#flow-new-password").fill("brand-new-password");
    await dialog.locator("#account-flow-submit").click();

    await expect(dialog).not.toBeVisible();
    await expect(page.locator("#status")).toContainText("Password reset");
    await expect(page.locator("#username")).toHaveValue("alice");
    await expect(page.locator("#password")).toHaveValue("brand-new-password");
    expect(resetRequests).toBe(2);
    expect(received.map((packet) => packet.type)).toEqual([
      "request_password_reset",
      "reset_password",
      "request_password_reset",
      "reset_password",
    ]);
  });
}

for (const { state, message } of EMAIL_CHANGE_ERRORS.filter(
  (entry) => entry.state !== "invalid"
)) {
  test(`email change completes with a resent code after ${state === "expired" ? "an" : "a"} ${state} code`, async ({
    page,
  }) => {
    let changeRequests = 0;
    const { dialog, received } = await connectAndOpenAccountSettings(
      page,
      (packet, ws) => {
        if (packet.type === "request_email_change") {
          changeRequests += 1;
          send(ws, {
            type: "email_change_pending",
            email: packet.email,
            message:
              changeRequests === 1
                ? "A confirmation code was sent to new@example.com."
                : "A new confirmation code was sent to new@example.com.",
            expires_in: 900,
          });
        } else if (packet.type === "verify_email_change") {
          if (changeRequests === 1) {
            expect(packet.code).toBe("OLDCODE");
            send(ws, {
              type: "email_change_failed",
              message,
              attempts_remaining: state === "locked" ? 0 : 4,
            });
          } else {
            expect(packet.code).toBe("NEWCODE");
            send(ws, {
              type: "email_change_complete",
              email: "new@example.com",
              message: "Email address updated.",
            });
          }
        }
      }
    );

    await dialog.locator("#account-settings-email").fill("new@example.com");
    await dialog.locator("#account-settings-send").click();
    await expect(dialog.locator("#account-settings-status")).toContainText(
      "confirmation code"
    );
    await dialog.locator("#account-settings-code").fill("OLDCODE");
    await dialog.locator("#account-settings-verify").click();
    await expect(dialog.locator("#account-settings-status")).toHaveText(message);

    await dialog.locator("#account-settings-send").click();
    await expect(dialog.locator("#account-settings-status")).toHaveText(
      "A new confirmation code was sent to new@example.com."
    );

    await dialog.locator("#account-settings-code").fill("NEWCODE");
    await dialog.locator("#account-settings-verify").click();

    await expect(dialog.locator("#account-settings-status")).toHaveText(
      "Email address updated."
    );
    await expect(dialog.locator("#account-settings-current")).toContainText(
      "new@example.com"
    );
    expect(changeRequests).toBe(2);
    expect(received.map((packet) => packet.type)).toEqual([
      "authorize",
      "request_email_change",
      "verify_email_change",
      "request_email_change",
      "verify_email_change",
    ]);
  });
}

test("account settings disables actions when email delivery is unavailable", async ({
  page,
}) => {
  const { dialog, received } = await connectAndOpenAccountSettings(
    page,
    () => {},
    { available: false }
  );
  const status = dialog.locator("#account-settings-status");

  await expect(status).toHaveText(
    "Email settings are not available on this server."
  );
  await expect(status).toHaveClass(/error/);
  await expect(dialog.locator("#account-settings-send")).toBeDisabled();
  await expect(dialog.locator("#account-settings-verify")).toBeDisabled();
  await expect(dialog.locator("#account-settings-close")).toBeEnabled();
  await expect(dialog.locator("#account-settings-email")).toHaveValue("");
  expect(received.map((packet) => packet.type)).toEqual(["authorize"]);
});

test("account settings can retry when email delivery fails", async ({ page }) => {
  let changeRequests = 0;
  const { dialog, received } = await connectAndOpenAccountSettings(
    page,
    (packet, ws) => {
      if (packet.type !== "request_email_change") {
        return;
      }
      changeRequests += 1;
      if (changeRequests === 1) {
        send(ws, {
          type: "email_change_failed",
          message: "The confirmation email could not be sent. Try again later.",
        });
      } else {
        send(ws, {
          type: "email_change_pending",
          email: packet.email,
          message: "A confirmation code was sent to new@example.com.",
          expires_in: 900,
        });
      }
    }
  );

  await dialog.locator("#account-settings-email").fill("new@example.com");
  await dialog.locator("#account-settings-send").click();

  const status = dialog.locator("#account-settings-status");
  await expect(status).toHaveText(
    "The confirmation email could not be sent. Try again later."
  );
  await expect(status).toHaveClass(/error/);
  await expect(dialog.locator("#account-settings-email")).toHaveValue(
    "new@example.com"
  );
  await expect(dialog.locator("#account-settings-send")).toBeEnabled();

  await dialog.locator("#account-settings-send").click();
  await expect(status).toHaveText(
    "A confirmation code was sent to new@example.com."
  );
  await expect(status).not.toHaveClass(/error/);
  expect(changeRequests).toBe(2);
  expect(received.map((packet) => packet.type)).toEqual([
    "authorize",
    "request_email_change",
    "request_email_change",
  ]);
});
