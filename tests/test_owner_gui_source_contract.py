"""Camera-free owner GUI contract, separate from the actual installer-release gate."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "windows-alpha" / "owner-installer" / "K5VisionAlpha.cs"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-alpha-script-smoke.yml"


class OwnerGuiContract(unittest.TestCase):
    def test_gui_not_a_console_or_shell_handoff(self):
        source = SOURCE.read_text(encoding="utf-8")
        for marker in (
            "Application.Run(new Launcher())",
            "info.CreateNoWindow = true;",
            "info.RedirectStandardInput = true;",
            "info.RedirectStandardOutput = true;",
            "info.RedirectStandardError = true;",
            "-ExitAfterPublicTest",
            "uri.UserInfo.Length != 0",
            "Private or local camera addresses",
            "Recording is disabled",
            "--self-check",
            "File.Exists(gst)",
        ):
            if marker == "Private or local camera addresses":
                marker = "private or local camera addresses"
            self.assertIn(marker, source)
        self.assertNotIn("Status(line)", source)
        self.assertNotIn("Status(e.Data)", source)
        self.assertNotIn("MessageBox.Show(this, e.Data", source)
        self.assertNotIn("UseShellExecute = true", source)

    def test_gui_requires_positive_receipts_not_just_zero_process_exit(self):
        source = SOURCE.read_text(encoding="utf-8")
        for marker in (
            "finished.WaitForExit()",
            "healthEvidence && presentationEvidence && privacyEvidence",
            "video && analyticsEvidence && boxesEvidence",
            "analyticsEvidence = true",
            "boxesEvidence = true",
            "presentationEvidence = true",
            "Stage-One NOT qualified:",
            "public RTSP analytics NOT qualified.",
            "No test-stream recording or retained media was created.",
            "submissions=([1-9][0-9]*)",
            "presentations=([1-9][0-9]*)",
        ):
            self.assertIn(marker, source)
        self.assertNotIn('result == 0 ? "Test completed."', source)
        self.assertIn(
            "bool full = source.Length > 0 && video && analyticsEvidence && boxesEvidence;",
            source,
        )
        self.assertNotIn("bool full = video && analyticsEvidence && boxesEvidence;", source)

    def test_gui_explains_missing_analytics_to_owner(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("K5 analytics disabled; video-only alpha acceptance selected.", source)
        self.assertIn("Analytics is not provisioned or configured.", source)
        self.assertIn("Status(failure);", source)

    def test_packaged_analytics_is_selected_from_gui_without_owner_setup(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('Path.Combine(root, "analytics-config.json")', source)
        self.assertIn("ConfigureAnalyticsForTest(info, root, source.Length != 0);", source)
        self.assertNotIn("if (File.Exists(installedAnalyticsConfig))", source)
        self.assertIn(
            'info.EnvironmentVariables["K5_ANALYTICS_CONFIG"] = installedAnalyticsConfig;', source
        )

    def test_synthetic_mode_does_not_change_public_acceptance_guards(self):
        source = SOURCE.read_text(encoding="utf-8")
        helper = source.split("private static void ConfigureAnalyticsForTest", 1)[1].split(
            "private static string Quote", 1
        )[0]
        self.assertIn('info.EnvironmentVariables.Remove("K5_ANALYTICS_CONFIG");', helper)
        self.assertIn("if (publicSource)", helper)
        self.assertNotIn("Environment.SetEnvironmentVariable", helper)
        self.assertNotIn("File.Exists", helper)
        self.assertIn("Synthetic video-only mode selected;", source)
        self.assertIn("source.Length == 0 && video", source)
        self.assertIn(
            "bool full = source.Length > 0 && video && analyticsEvidence && boxesEvidence;",
            source,
        )

    def test_decoder_failure_has_a_fixed_gui_message_and_priority(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("failure = SelectRuntimeFailure(failure, e.Data);", source)
        helper = source.split("private static string SelectRuntimeFailure", 1)[1].split(
            "private void Status", 1
        )[0]
        self.assertIn("previous == DecoderFailure", helper)
        self.assertLess(helper.index('"decoder"'), helper.index('"RTSP"'))
        self.assertNotIn("return line;", helper)
        self.assertIn("K5-DECODER:", source)
        self.assertIn("changing the stream address alone may not resolve it", source)

    def test_installed_operator_failure_class_is_bounded_and_source_free(self):
        source = SOURCE.read_text(encoding="utf-8")
        launcher = (ROOT / "scripts" / "windows-alpha" / "Start-K5VisionAlpha.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("K5_OPERATOR_LIVE_FAILURE=", launcher)
        self.assertIn("$count -le 2048", launcher)
        self.assertIn("$detail.Length -le 1024", launcher)
        self.assertIn("$response.StatusCode -eq 503", launcher)
        self.assertIn('throw "K5 Windows operator live request failed."', launcher)
        self.assertNotIn("Write-Host $detail", launcher)
        self.assertNotIn("Write-Host $body", launcher)
        self.assertIn('line.StartsWith("K5_OPERATOR_LIVE_FAILURE="', source)
        for code in (
            "source_unavailable",
            "analytics_start_failure",
            "unconfigured",
            "execution_failure",
            "start_failure",
            "assembly_failure",
            "cleanup_failure",
            "launch_failure",
            "unknown",
        ):
            self.assertIn("K5_OPERATOR_LIVE_FAILURE=" + code, source)
        self.assertNotIn("Status(line)", source)

    @unittest.skipUnless(sys.platform == "win32", "Requires real Windows C# compiler")
    def test_windows_child_analytics_mode_is_explicit_and_parent_unchanged(self):
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        harness = r"""
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
internal static class OwnerModeProbe
{
    public static int Main()
    {
        string prior = Environment.GetEnvironmentVariable("K5_ANALYTICS_CONFIG");
        try
        {
            Environment.SetEnvironmentVariable("K5_ANALYTICS_CONFIG", "untrusted-inherited-config");
            Type launcher = typeof(OwnerModeProbe).Assembly.GetType("K5VisionAlpha.Launcher");
            MethodInfo select = launcher.GetMethod("ConfigureAnalyticsForTest",
                BindingFlags.NonPublic | BindingFlags.Static);
            if (select == null) return 10;
            string root = Path.Combine(Path.GetTempPath(), "K5 fixture root with spaces");
            var child = new ProcessStartInfo();
            select.Invoke(null, new object[] { child, root, false });
            if (child.EnvironmentVariables.ContainsKey("K5_ANALYTICS_CONFIG")) return 11;
            select.Invoke(null, new object[] { child, root, true });
            if (child.EnvironmentVariables["K5_ANALYTICS_CONFIG"] !=
                Path.Combine(root, "analytics-config.json")) return 12;
            // Missing public config must still be selected, so installed preflight
            // fails closed rather than silently accepting video-only output.
            select.Invoke(null, new object[] { child, root, false });
            if (child.EnvironmentVariables.ContainsKey("K5_ANALYTICS_CONFIG")) return 13;
            if (Environment.GetEnvironmentVariable("K5_ANALYTICS_CONFIG") !=
                "untrusted-inherited-config") return 14;
            MethodInfo failure = launcher.GetMethod("SelectRuntimeFailure",
                BindingFlags.NonPublic | BindingFlags.Static);
            if (failure == null) return 16;
            string chosen = (string)failure.Invoke(null, new object[] {
                "initial", "GStreamer RTSP DECODER failed private-fixture-marker" });
            if (!chosen.StartsWith("K5-DECODER:", StringComparison.Ordinal) ||
                chosen.Contains("private-fixture-marker")) return 17;
            foreach (string line in new string[] { "RTSP failed", "health failed", "", null })
            {
                if ((string)failure.Invoke(null, new object[] { chosen, line }) != chosen)
                    return 18;
            }
            if ((string)failure.Invoke(null, new object[] {
                "initial", "unknown private-fixture-marker" }) != "initial") return 19;
            string reset = (string)failure.Invoke(null, new object[] {
                "new test", "health failed" });
            if (reset == chosen || !reset.Contains("health")) return 20;
            string named = (string)failure.Invoke(null, new object[] {
                "initial", "d3d11h264dec unavailable" });
            if (named != chosen) return 21;
            string classed = (string)failure.Invoke(null, new object[] {
                "initial", "K5_OPERATOR_LIVE_FAILURE=execution_failure" });
            if (!classed.Contains("execution failed")) return 22;
            if ((string)failure.Invoke(null, new object[] {
                "initial", "K5_OPERATOR_LIVE_FAILURE=private-fixture-marker" }) != "initial")
                return 23;
            Console.WriteLine("K5_OWNER_ANALYTICS_MODE_CONTRACT=passed");
            return 0;
        }
        catch { return 15; }
        finally { Environment.SetEnvironmentVariable("K5_ANALYTICS_CONFIG", prior); }
    }
}
"""
        with tempfile.TemporaryDirectory(prefix="k5-owner-mode-") as directory:
            temporary = Path(directory)
            probe = temporary / "OwnerModeProbe.cs"
            executable = temporary / "OwnerModeProbe.exe"
            probe.write_text(harness, encoding="utf-8")
            compiled = subprocess.run(
                [
                    str(compiler),
                    "/nologo",
                    "/target:exe",
                    "/main:OwnerModeProbe",
                    "/reference:System.Windows.Forms.dll",
                    "/reference:System.Drawing.dll",
                    f"/out:{executable}",
                    str(SOURCE),
                    str(probe),
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(compiled.returncode, 0, "Owner GUI mode probe compilation failed")
            result = subprocess.run([str(executable)], capture_output=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, "Owner GUI child mode isolation failed")
            self.assertEqual(result.stdout.strip(), b"K5_OWNER_ANALYTICS_MODE_CONTRACT=passed")

    def test_windows_hosted_compiler_is_mandatory_for_gui_changes(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("scripts/windows-alpha/**", workflow)
        self.assertIn("Compile camera-free owner GUI candidate", workflow)
        self.assertIn("K5VisionAlpha.cs", workflow)
        self.assertIn("K5_OWNER_GUI_HOSTED_COMPILE=passed", workflow)
        self.assertIn(
            "Start-Process -FilePath $output -ArgumentList '--self-check' -Wait -PassThru",
            workflow,
        )
        self.assertIn("if ($guiProbe.ExitCode -ne 3)", workflow)
        self.assertIn("$env:GITHUB_WORKSPACE", workflow)
        self.assertIn("'scripts\\windows-alpha\\owner-installer\\K5VisionAlpha.cs'", workflow)


if __name__ == "__main__":
    unittest.main()
