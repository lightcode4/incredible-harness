package com.lightcode4.plugin;

import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.wm.ToolWindowManager;
import org.jetbrains.annotations.NotNull;

import java.util.Map;

/** Starts a LightCode⁴ session in this project's folder and shows the tool window. */
public class OpenAction extends AnAction {
    @Override
    public void actionPerformed(@NotNull AnActionEvent e) {
        Project project = e.getProject();
        if (project == null) return;
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                LightCodeApp.post(LightCodeApp.ensure(), "/api/editor/open", Map.of("cwd", project.getBasePath() == null ? "" : project.getBasePath()));
            } catch (Exception ignored) {
            }
            ApplicationManager.getApplication().invokeLater(() -> {
                var tw = ToolWindowManager.getInstance(project).getToolWindow("LightCode⁴");
                if (tw != null) tw.activate(null);
            });
        });
    }
}
