package com.lightcode4.plugin;

import com.intellij.notification.NotificationGroupManager;
import com.intellij.notification.NotificationType;
import com.intellij.openapi.actionSystem.ActionUpdateThread;
import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import com.intellij.openapi.actionSystem.CommonDataKeys;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.editor.Editor;
import com.intellij.openapi.editor.SelectionModel;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.vfs.VirtualFile;
import com.intellij.openapi.wm.ToolWindowManager;
import org.jetbrains.annotations.NotNull;

import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/** Puts the selected code (with its file and lines) into LightCode⁴'s message box. */
public class AskAction extends AnAction {
    @Override
    public @NotNull ActionUpdateThread getActionUpdateThread() { return ActionUpdateThread.BGT; }

    @Override
    public void update(@NotNull AnActionEvent e) {
        e.getPresentation().setEnabledAndVisible(e.getData(CommonDataKeys.EDITOR) != null && e.getProject() != null);
    }

    @Override
    public void actionPerformed(@NotNull AnActionEvent e) {
        Project project = e.getProject();
        Editor editor = e.getData(CommonDataKeys.EDITOR);
        VirtualFile file = e.getData(CommonDataKeys.VIRTUAL_FILE);
        if (project == null || editor == null) return;
        SelectionModel sel = editor.getSelectionModel();
        String text = sel.hasSelection() ? sel.getSelectedText() : editor.getDocument().getText();
        int start = editor.getDocument().getLineNumber(sel.getSelectionStart()) + 1;
        int end = editor.getDocument().getLineNumber(sel.getSelectionEnd()) + 1;
        String base = project.getBasePath() == null ? "" : project.getBasePath();
        String rel = file == null ? "" : (base.isEmpty() ? file.getName() : Path.of(base).relativize(Path.of(file.getPath())).toString());
        String lang = file == null || file.getExtension() == null ? "" : file.getExtension();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("cwd", base);
        body.put("file", rel);
        body.put("text", text == null ? "" : text);
        body.put("lang", lang);
        body.put("start", start);
        body.put("end", end);
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                LightCodeApp.post(LightCodeApp.ensure(), "/api/editor/ask", body);
                ApplicationManager.getApplication().invokeLater(() -> {
                    var tw = ToolWindowManager.getInstance(project).getToolWindow("LightCode⁴");
                    if (tw != null) tw.activate(null);
                });
            } catch (Exception ex) {
                NotificationGroupManager.getInstance().getNotificationGroup("LightCode4")
                        .createNotification("LightCode⁴: " + ex.getMessage(), NotificationType.WARNING).notify(project);
            }
        });
    }
}
