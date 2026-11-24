package com.lightcode4.plugin;

import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.project.DumbAware;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.wm.ToolWindow;
import com.intellij.openapi.wm.ToolWindowFactory;
import com.intellij.ui.content.Content;
import com.intellij.ui.content.ContentFactory;
import com.intellij.ui.jcef.JBCefApp;
import com.intellij.ui.jcef.JBCefBrowser;
import org.jetbrains.annotations.NotNull;

import javax.swing.*;
import java.awt.*;
import java.util.Map;

/** The LightCode⁴ app inside the IDE, in a tool window on the right. */
public class LightCodeToolWindow implements ToolWindowFactory, DumbAware {
    static JBCefBrowser browser;

    @Override
    public void createToolWindowContent(@NotNull Project project, @NotNull ToolWindow toolWindow) {
        JPanel holder = new JPanel(new BorderLayout());
        JLabel waiting = new JLabel("Starting LightCode⁴…", SwingConstants.CENTER);
        holder.add(waiting, BorderLayout.CENTER);
        Content content = ContentFactory.getInstance().createContent(holder, "", false);
        toolWindow.getContentManager().addContent(content);
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                LightCodeApp.Running r = LightCodeApp.ensure();
                if (project.getBasePath() != null) LightCodeApp.post(r, "/api/editor/open", Map.of("cwd", project.getBasePath()));
                SwingUtilities.invokeLater(() -> {
                    holder.removeAll();
                    if (JBCefApp.isSupported()) {
                        browser = new JBCefBrowser(r.page());
                        holder.add(browser.getComponent(), BorderLayout.CENTER);
                    } else {
                        holder.add(new JLabel("This IDE can't show web pages; LightCode⁴ opens in its own window.", SwingConstants.CENTER));
                    }
                    holder.revalidate();
                });
            } catch (Exception e) {
                SwingUtilities.invokeLater(() -> waiting.setText(e.getMessage()));
            }
        });
    }
}
