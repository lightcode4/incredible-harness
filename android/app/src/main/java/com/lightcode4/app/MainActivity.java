package com.lightcode4.app;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import com.google.mlkit.vision.barcode.common.Barcode;
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions;
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning;

import org.json.JSONObject;

/**
 * LightCode⁴ for Android. One screen, three places:
 *  - home.html: pair with your computer (QR code) or start a chat on the phone
 *  - your computer's LightCode⁴ (its phone page, over Wi-Fi or the "access from anywhere" link)
 *  - chat.html: chat on the phone itself with your own API key
 */
public class MainActivity extends Activity {
    static final String PREFS = "lc4";
    private WebView web;
    private boolean onRemote = false;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#16120D"));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        web.addJavascriptInterface(new Bridge(), "LC4");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
                Uri u = req.getUrl();
                String scheme = u.getScheme() == null ? "" : u.getScheme();
                boolean mine = scheme.equals("file") || u.toString().startsWith(remoteBase());
                if (mine) return false;
                startActivity(new Intent(Intent.ACTION_VIEW, u));  // other links open in the phone's browser
                return true;
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest r) {  // voice typing on the computer's page
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED)
                    requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 2);
                r.grant(r.getResources());
            }
        });
        setContentView(web);
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        if (prefs().getBoolean("watch", false) && !prefs().getString("remote", "").isEmpty()) WatchService.start(this);
        handle(getIntent());
    }

    @Override
    protected void onNewIntent(Intent i) {
        super.onNewIntent(i);
        handle(i);
    }

    private void handle(Intent i) {
        if (i != null && "remote".equals(i.getStringExtra("open"))) openRemote();
        else home();
    }

    SharedPreferences prefs() { return getSharedPreferences(PREFS, Context.MODE_PRIVATE); }

    String remoteBase() {
        String r = prefs().getString("remote", "");
        Uri u = Uri.parse(r);
        return r.isEmpty() || u.getHost() == null ? "\u0000" : u.getScheme() + "://" + u.getAuthority();
    }

    void home() { onRemote = false; web.loadUrl("file:///android_asset/home.html"); }

    void openRemote() {
        String r = prefs().getString("remote", "");
        if (r.isEmpty()) { home(); return; }
        onRemote = true;
        web.loadUrl(r);
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack() && !onRemote) web.goBack();
        else if (onRemote || !web.getUrl().endsWith("home.html")) home();
        else super.onBackPressed();
    }

    /** Functions the pages call as LC4.name(...). */
    class Bridge {
        @JavascriptInterface public String get(String key) { return prefs().getString(key, ""); }
        @JavascriptInterface public void set(String key, String value) { prefs().edit().putString(key, value).apply(); }
        @JavascriptInterface public int build() { return BuildConfig.LC4_BUILD; }
        @JavascriptInterface public boolean watching() { return prefs().getBoolean("watch", false); }

        @JavascriptInterface public void watch(boolean on) {
            prefs().edit().putBoolean("watch", on).apply();
            runOnUiThread(() -> { if (on) WatchService.start(MainActivity.this); else WatchService.stop(MainActivity.this); });
        }

        @JavascriptInterface public void pair(String link) {
            String clean = link == null ? "" : link.trim();
            prefs().edit().putString("remote", clean).apply();
            if (prefs().getBoolean("watch", false)) runOnUiThread(() -> WatchService.start(MainActivity.this));
        }

        @JavascriptInterface public void unpair() {
            prefs().edit().remove("remote").apply();
            runOnUiThread(() -> WatchService.stop(MainActivity.this));
        }

        @JavascriptInterface public void openRemote() { runOnUiThread(MainActivity.this::openRemote); }
        @JavascriptInterface public void openChat() { runOnUiThread(() -> { onRemote = false; web.loadUrl("file:///android_asset/chat.html"); }); }
        @JavascriptInterface public void goHome() { runOnUiThread(MainActivity.this::home); }

        @JavascriptInterface public void openUrl(String url) {
            runOnUiThread(() -> startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url))));
        }

        @JavascriptInterface public void copy(String text) {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            cm.setPrimaryClip(ClipData.newPlainText("LightCode⁴", text));
        }

        /** Streams an HTTPS request for the on-phone chat; each line of the reply goes to onStreamLine(id, line). */
        @JavascriptInterface public void stream(String id, String url, String headersJson, String body) {
            Thread t = new Thread(() -> {
                int status = 0;
                String err = "";
                java.net.HttpURLConnection c = null;
                try {
                    c = (java.net.HttpURLConnection) new java.net.URL(url).openConnection();
                    c.setRequestMethod("POST");
                    c.setDoOutput(true);
                    c.setConnectTimeout(20000);
                    c.setReadTimeout(180000);
                    JSONObject h = new JSONObject(headersJson);
                    for (java.util.Iterator<String> it = h.keys(); it.hasNext(); ) { String k = it.next(); c.setRequestProperty(k, h.getString(k)); }
                    streams.put(id, c);
                    try (java.io.OutputStream o = c.getOutputStream()) { o.write(body.getBytes(java.nio.charset.StandardCharsets.UTF_8)); }
                    status = c.getResponseCode();
                    java.io.InputStream in = status < 400 ? c.getInputStream() : c.getErrorStream();
                    try (java.io.BufferedReader r = new java.io.BufferedReader(new java.io.InputStreamReader(in, java.nio.charset.StandardCharsets.UTF_8))) {
                        String line;
                        while ((line = r.readLine()) != null) jsPair("onStreamLine", id, line);
                    }
                } catch (Exception e) {
                    err = streams.containsKey(id) ? (e.getMessage() == null ? e.toString() : e.getMessage()) : "stopped";
                } finally {
                    streams.remove(id);
                    if (c != null) c.disconnect();
                }
                try { jsPair("onStreamEnd", id, new JSONObject().put("status", status).put("error", err).toString()); } catch (Exception ignored) {}
            }, "lc4-stream");
            t.start();
        }

        @JavascriptInterface public void cancel(String id) {
            java.net.HttpURLConnection c = streams.remove(id);
            if (c != null) new Thread(c::disconnect).start();
        }

        @JavascriptInterface public void scan() {
            runOnUiThread(() -> {
                GmsBarcodeScannerOptions opts = new GmsBarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build();
                GmsBarcodeScanning.getClient(MainActivity.this, opts).startScan()
                        .addOnSuccessListener(b -> js("onScan", b.getRawValue()))
                        .addOnFailureListener(e -> js("onScanError", e.getMessage() == null ? "Couldn't open the scanner" : e.getMessage()));
            });
        }
    }

    final java.util.concurrent.ConcurrentHashMap<String, java.net.HttpURLConnection> streams = new java.util.concurrent.ConcurrentHashMap<>();

    void jsPair(String fn, String a, String b) {
        String x = JSONObject.quote(a), y = JSONObject.quote(b);
        web.post(() -> web.evaluateJavascript("window." + fn + " && " + fn + "(" + x + "," + y + ")", null));
    }

    void js(String fn, String arg) {
        String a = JSONObject.quote(arg == null ? "" : arg);
        web.post(() -> web.evaluateJavascript("window." + fn + " && " + fn + "(" + a + ")", null));
    }
}
