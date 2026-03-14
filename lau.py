name: Update Reuters RSS

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:

jobs:
  update:
    runs-on: ubuntu-latest

    permissions:
      contents: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install Python dependencies
        run: |
          python -m pip install --upgrade pip
          pip install requests beautifulsoup4 playwright

      # Your existing get_bb_tag.py script should be in the repo
      - name: Get latest BotBrowser tag
        id: botbrowser
        run: |
          TAG=$(python3 get_bb_tag.py)
          echo "tag=$TAG" >> $GITHUB_OUTPUT

      - name: Download BotBrowser binary
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          TAG="${{ steps.botbrowser.outputs.tag }}"
          # Fetch asset list from GitHub API
          ASSETS_JSON=$(curl -s -H "Authorization: Bearer $GH_TOKEN" \
            "https://api.github.com/repos/botswin/BotBrowser/releases/tags/$TAG")

          # Find the Linux x64 binary asset (not .deb, .snap, .enc.zip)
          BINARY_ASSET=$(echo "$ASSETS_JSON" | jq -r '.assets[] | select(.name | test("linux.*x64") and (test("\\.deb$|\\.snap$|\\.enc\\.zip$") | not)) | .name' | head -1)

          if [ -z "$BINARY_ASSET" ]; then
            echo "ERROR: Could not find Linux x64 binary asset"
            exit 1
          fi

          echo "Downloading binary asset: $BINARY_ASSET"
          DOWNLOAD_URL="https://github.com/botswin/BotBrowser/releases/download/${TAG}/${BINARY_ASSET}"
          curl -fL -H "Authorization: Bearer $GH_TOKEN" "$DOWNLOAD_URL" -o botbrowser

          chmod +x botbrowser
          echo "BOTBROWSER_PATH=$PWD/botbrowser" >> $GITHUB_ENV
          ./botbrowser --version || true

      - name: Download BotBrowser profile (optional)
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          TAG="${{ steps.botbrowser.outputs.tag }}"
          PROFILE_ASSET="botbrowser-linux-x64.enc.zip"
          DOWNLOAD_URL="https://github.com/botswin/BotBrowser/releases/download/${TAG}/${PROFILE_ASSET}"
          if curl -fL -H "Authorization: Bearer $GH_TOKEN" --output /dev/null --silent --head "$DOWNLOAD_URL"; then
            echo "Profile asset found, downloading..."
            curl -fL -H "Authorization: Bearer $GH_TOKEN" "$DOWNLOAD_URL" -o profile.zip
            unzip -q profile.zip
            PROFILE_FILE=$(ls *.enc 2>/dev/null | head -1 || true)
            if [ -n "$PROFILE_FILE" ]; then
              echo "BOTBROWSER_PROFILE=$PWD/$PROFILE_FILE" >> $GITHUB_ENV
              echo "Using profile: $PROFILE_FILE"
            else
              echo "No .enc file found after extraction"
            fi
          else
            echo "No profile asset found for this release"
          fi

      - name: Start FlareSolverr
        run: |
          docker run -d --name flaresolverr -p 8191:8191 -e LOG_LEVEL=info ghcr.io/flaresolverr/flaresolverr:latest

      - name: Wait for FlareSolverr
        run: |
          for i in {1..30}; do
            if curl -s http://localhost:8191/v1 >/dev/null; then
              echo "FlareSolverr is up"
              break
            fi
            echo "Waiting... ($i)"
            sleep 3
          done

      - name: Run scraper
        env:
          BOTBROWSER_PATH: ${{ env.BOTBROWSER_PATH }}
          BOTBROWSER_PROFILE: ${{ env.BOTBROWSER_PROFILE }}
          BOTBROWSER_CDP_PORT: "9222"
        run: |
          python lau.py

      - name: Commit and push changes
        run: |
          git config user.name "github-actions"
          git config user.email "github-actions@github.com"

          git add *.xml opinin.html commentary.html apnews.html debug.log || true

          if git diff --cached --quiet; then
            echo "Nothing to commit"
            exit 0
          fi

          git commit -m "Auto update RSS [$(date -u '+%Y-%m-%d %H:%M UTC')]"
          git pull --rebase origin main
          git push origin main