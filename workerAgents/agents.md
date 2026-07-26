# Worker Agents instructions

- Keep this project generic: do not reintroduce Android, APK, Gradle, fastlane, Play Store, Shizuku, or device-build workflow instructions.
- Prefer small Node.js changes and verify with `npm run check`.
- Built-in workers are examples; keep custom worker support first-class through environment variables and `workers.json`.
- Use `rg` for repo searches.
