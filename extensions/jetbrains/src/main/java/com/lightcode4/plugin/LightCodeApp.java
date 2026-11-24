package com.lightcode4.plugin;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Finds the LightCode⁴ app running on this computer (~/.incredible-herness/running.json) and talks to it. */
public final class LightCodeApp {
    private static final HttpClient HTTP = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();

    public record Running(String url, String token) {
        public String page() { return url + "/?token=" + token; }
    }

    private LightCodeApp() {}

    static Running read() {
        try {
            String json = Files.readString(Path.of(System.getProperty("user.home"), ".incredible-herness", "running.json"));
            String url = field(json, "url"), token = field(json, "token");
            return url == null || token == null ? null : new Running(url, token);
        } catch (IOException e) {
            return null;
        }
    }

    private static String field(String json, String name) {
        Matcher m = Pattern.compile("\"" + name + "\"\\s*:\\s*\"([^\"]*)\"").matcher(json);
        return m.find() ? m.group(1) : null;
    }

    static boolean alive(Running r) {
        if (r == null) return false;
        try {
            HttpResponse<Void> res = HTTP.send(HttpRequest.newBuilder(URI.create(r.url() + "/api/ping?token=" + r.token()))
                    .timeout(Duration.ofSeconds(2)).GET().build(), HttpResponse.BodyHandlers.discarding());
            return res.statusCode() < 500;
        } catch (Exception e) {
            return false;
        }
    }

    /** The running app, starting it if needed (waits up to 20 seconds). */
    static Running ensure() throws IOException {
        Running r = read();
        if (alive(r)) return r;
        String os = System.getProperty("os.name").toLowerCase();
        ProcessBuilder pb = os.contains("mac") ? new ProcessBuilder("open", "-a", "LightCode4")
                : os.contains("win") ? new ProcessBuilder("cmd", "/c", "start", "", "lightcode4://open")
                : new ProcessBuilder("lightcode4");
        pb.start();
        for (int i = 0; i < 40; i++) {
            try { Thread.sleep(500); } catch (InterruptedException e) { Thread.currentThread().interrupt(); break; }
            r = read();
            if (alive(r)) return r;
        }
        throw new IOException("LightCode⁴ isn't running. Open the LightCode⁴ app, then try again.");
    }

    static void post(Running r, String route, Map<String, Object> body) throws IOException {
        StringBuilder sb = new StringBuilder("{");
        for (Map.Entry<String, Object> e : body.entrySet()) {
            if (sb.length() > 1) sb.append(',');
            sb.append(quote(e.getKey())).append(':');
            Object v = e.getValue();
            sb.append(v instanceof Number ? v.toString() : quote(String.valueOf(v)));
        }
        sb.append('}');
        try {
            HttpResponse<String> res = HTTP.send(HttpRequest.newBuilder(URI.create(r.url() + route))
                    .timeout(Duration.ofSeconds(15)).header("Content-Type", "application/json").header("X-Token", r.token())
                    .POST(HttpRequest.BodyPublishers.ofString(sb.toString())).build(), HttpResponse.BodyHandlers.ofString());
            if (res.statusCode() >= 400) throw new IOException("LightCode⁴ said: " + res.body());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IOException("Interrupted");
        }
    }

    static String quote(String s) {
        StringBuilder out = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                default -> { if (c < 0x20) out.append(String.format("\\u%04x", (int) c)); else out.append(c); }
            }
        }
        return out.append('"').toString();
    }
}
