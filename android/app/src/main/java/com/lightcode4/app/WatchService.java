package com.lightcode4.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Keeps a quiet connection to your computer's LightCode⁴ and turns its events into phone notifications:
 * "Approval needed" when the agent is waiting for you, and "Done" when a reply finishes.
 */
public class WatchService extends Service {
    static final String QUIET = "lc4_quiet", ALERTS = "lc4_alerts";
    private volatile boolean running;
    private Thread worker;
    private int nextId = 100;

    static void start(Context c) {
        Intent i = new Intent(c, WatchService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i); else c.startService(i);
    }

    static void stop(Context c) { c.stopService(new Intent(c, WatchService.class)); }

    @Override public IBinder onBind(Intent i) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        channels();
        Notification n = base(QUIET).setContentTitle("Connected to your computer")
                .setContentText("You'll be told when LightCode⁴ needs you.").setOngoing(true).build();
        if (Build.VERSION.SDK_INT >= 34) startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        else startForeground(1, n);
        if (worker == null || !worker.isAlive()) {
            running = true;
            worker = new Thread(this::loop, "lc4-watch");
            worker.start();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }

    private void loop() {
        int backoff = 2;
        while (running) {
            String remote = getSharedPreferences(MainActivity.PREFS, MODE_PRIVATE).getString("remote", "");
            if (remote.isEmpty()) { stopSelf(); return; }
            try {
                listen(remote);
                backoff = 2;
            } catch (Exception e) {
                try { Thread.sleep(backoff * 1000L); } catch (InterruptedException ie) { return; }
                backoff = Math.min(backoff * 2, 60);
            }
        }
    }

    /** Reads the computer's event stream (/api/events) until it closes. */
    private void listen(String remote) throws Exception {
        android.net.Uri u = android.net.Uri.parse(remote);
        String token = u.getQueryParameter("token");
        URL url = new URL(u.getScheme() + "://" + u.getAuthority() + "/api/events?token=" + token);
        HttpURLConnection c = (HttpURLConnection) url.openConnection();
        c.setConnectTimeout(10000);
        c.setReadTimeout(60000);  // the computer sends a keep-alive every 15 seconds
        try (BufferedReader r = new BufferedReader(new InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8))) {
            boolean first = true;
            String line;
            while (running && (line = r.readLine()) != null) {
                if (!line.startsWith("data: ")) continue;
                JSONObject ev = new JSONObject(line.substring(6));
                String kind = ev.optString("kind");
                if (first && !kind.equals("config")) first = false;
                if (kind.equals("approve")) {
                    alert("Approval needed", ev.optString("summary", "LightCode⁴ is waiting for you."));
                } else if (kind.equals("turn_end") && ev.optDouble("secs", 0) >= 20) {
                    alert("Done", "A reply on your computer just finished.");
                }
            }
        } finally {
            c.disconnect();
        }
    }

    private void alert(String title, String text) {
        Intent open = new Intent(this, MainActivity.class).putExtra("open", "remote").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification n = base(ALERTS).setContentTitle(title).setContentText(text).setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(pi).setAutoCancel(true).build();
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).notify(nextId++, n);
    }

    private Notification.Builder base(String channel) {
        return new Notification.Builder(this, channel).setSmallIcon(R.drawable.ic_stat).setColor(0xFFE8C46A);
    }

    private void channels() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel(QUIET, "Connection", NotificationManager.IMPORTANCE_MIN));
        nm.createNotificationChannel(new NotificationChannel(ALERTS, "Approvals and finished replies", NotificationManager.IMPORTANCE_HIGH));
    }
}
