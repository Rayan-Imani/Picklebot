# Railway Deployment Guide

This guide will walk you through deploying the Picklebot Discord bot to Railway.

## Prerequisites

- A Discord bot token (from Discord Developer Portal)
- OpenAI API key (from OpenAI)
- A GitHub account (for easy deployment)
- A Railway account (sign up at railway.app)

---

## Step 1: Create Discord Bot & Get Token

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **"New Application"** → Name it "Picklebot"
3. Go to the **"Bot"** tab
4. Click **"Add Bot"**
5. Under **TOKEN**, click **"Copy"** → Save this somewhere
6. Scroll down to **"Intents"** and enable:
   - ✅ Message Content Intent
7. Go to **"OAuth2"** → **"URL Generator"**
8. Select scopes: `bot`
9. Select permissions:
   - ✅ Send Messages
   - ✅ Use Slash Commands
10. Copy the generated URL and open it in your browser to invite the bot to your server

---

## Step 2: Prepare Your Code for Railway

### Option A: Using GitHub (Recommended)

1. Push your code to GitHub:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/PickleballCourtAI.git
   git push -u origin main
   ```

2. Make sure `.env` is in `.gitignore` (don't commit secrets!)
   ```
   # In .gitignore
   .env
   ```

### Option B: Direct Upload

You can also upload directly to Railway without GitHub.

---

## Step 3: Deploy to Railway

1. Go to [Railway.app](https://railway.app)
2. Sign in with GitHub (or email)
3. Click **"New Project"** → **"Deploy from GitHub"**
4. Select your `PickleballCourtAI` repository
5. Click **"Deploy"**

Railway will automatically:
- Detect the `Dockerfile`
- Build the bot image with Playwright Chromium and system dependencies included
- Start the bot through `start_discord_bot.sh`, which runs the bot with `xvfb-run` when no display is present

---

## Step 4: Set Environment Variables on Railway

1. Go to your Railway project dashboard
2. Click on the service name (or "new" if shown)
3. Go to the **"Variables"** tab
4. Add each variable from your `.env`:

   ```
   DISCORD_TOKEN=your_token_here
   OPENAI_API_KEY=your_key_here
   COURT_SITE_URL=https://your-court-site.com
   COURT_LOGIN_URL=https://your-court-site.com/login
   COURT_USERNAME=your_username
   COURT_PASSWORD=your_password
   COURT_LOGIN_USERNAME_SELECTOR=#login
   COURT_LOGIN_PASSWORD_SELECTOR=#password
   COURT_LOGIN_SUBMIT_SELECTOR=button[type='submit']
   COURT_AVAILABILITY_URL=https://your-court-site.com/availability
   COURT_AVAILABILITY_TABLE_SELECTOR=.availability-table
   ```

5. Click **"Deploy"** to restart with the new variables

Notes:
- Do not set `COURT_HEADLESS=true` if your reservation site blocks headless browsers.
- This repo uses a `Dockerfile` plus `start_discord_bot.sh` so both Docker and Procfile-based starts run through the same `xvfb-run` wrapper.

---

## Step 5: Verify Bot is Running

1. Go back to your Railway dashboard
2. Click your service
3. Go to the **"Logs"** tab
4. You should see: `Logged in as PickleBot#1234` and `Synced 1 command(s)`

---

## Step 6: Test Your Bot

1. Go to your Discord server
2. Type `/ask what's available tomorrow at 8pm?`
3. The bot should respond with available courts!

---

## Troubleshooting

### Bot doesn't show up in Discord
- Make sure the bot token is correct in Railway variables
- Make sure the bot is invited to your server

### `/ask` command doesn't work
- Check Railway logs for errors
- Make sure `OPENAI_API_KEY` is set correctly

### Playwright says Chromium executable doesn't exist
- Railway is still using an older non-Docker build, or the latest Docker image has not been rebuilt yet.
- This repo now uses a `Dockerfile` that installs Chromium during image build.
- Trigger a fresh redeploy after pushing the Dockerfile so Railway rebuilds from scratch.
- If the logs still mention Railpack or `Procfile`, Railway is still using the older deployment path.

### Browser launches but crashes on Railway
- If your site blocks headless browsers, leave `COURT_HEADLESS` unset or set it to `false`.
- This repo uses `xvfb-run` inside the Docker image so Chromium can run in headed mode inside a virtual display.
- The startup script is also wired into `Procfile`, so Railway should not bypass the virtual display wrapper.
- If you changed the `Dockerfile`, trigger a fresh redeploy so Railway rebuilds the image.

### "Could not understand" error
- Make sure your `COURT_*` environment variables are set
- Check that the CSS selectors match your court website

### Bot disconnects after a minute
- Usually means an error occurred. Check the logs in Railway

---

## Viewing Logs

To debug issues, check Railway logs:
1. Project Dashboard → Your Service → **Logs** tab
2. Look for errors and stack traces
3. Common issues will show up here

---

## Cost Tracking

You can see your usage and costs in Railway:
1. Billing → Resource Usage
2. Should show < $1 per month for a bot that runs a few times per week

---

## Auto-Restart on Code Push

If you used GitHub deployment:
- Every time you `git push` to your repository, Railway automatically redeploys
- Your bot will restart with the new code within ~30 seconds

---

## Need Help?

Check the logs in Railway for error messages. Most issues are:
1. Invalid API keys
2. Wrong CSS selectors for the court website
3. Bot token permissions not set correctly

Good luck! 🎾
