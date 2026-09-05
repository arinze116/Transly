module.exports = {
  apps: [{
    name: "transly",
    script: "bot.py",
    interpreter: "python3",
    watch: false,
    restart_delay: 5000,
    env: { PYTHONUNBUFFERED: "1" }
  }]
};