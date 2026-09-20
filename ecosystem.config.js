module.exports = {
  apps: [
    {
      name: 'tg-channel-tracker',
      script: '.venv/bin/python',
      args: '-m tracker run',
      cwd: '/home/ubuntu/TelegramTracker',
      instances: 1,
      autorestart: true,
      max_restarts: 20,
      min_uptime: '30s',
      restart_delay: 5000,
      max_memory_restart: '300M',
      out_file: '/home/ubuntu/TelegramTracker/logs/pm2-out.log',
      error_file: '/home/ubuntu/TelegramTracker/logs/pm2-err.log',
      merge_logs: true,
      time: true,
      env: {
        PYTHONUNBUFFERED: '1',
        // Token & chat id TIDAK di sini — sudah dibaca dari .env (lihat config.py).
      },
    },
  ],
};
