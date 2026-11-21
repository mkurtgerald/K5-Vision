// Native QA only: compile the actual four product sources and select this
// Main. Exercise real per-user/session kernel objects without product Main,
// forms, browser dispatch, service control, profile or filesystem ACL changes.
using System;
using System.Collections.Generic;
using System.Diagnostics;

namespace K5VisionDesktop
{
    internal static class ClientSingletonProbe
    {
        private static void Require(bool condition)
        {
            if (!condition) throw new InvalidOperationException();
        }

        private static void Child(string argument)
        {
            string executable;
            using (Process current = Process.GetCurrentProcess())
                executable = current.MainModule.FileName;
            ProcessStartInfo start = new ProcessStartInfo(executable, argument);
            start.UseShellExecute = false; start.CreateNoWindow = true;
            start.RedirectStandardOutput = true; start.RedirectStandardError = true;
            using (Process child = new Process { StartInfo = start })
            {
                Require(child.Start());
                // Keep the actual process object pinned throughout the bound.
                IntPtr owned = child.Handle;
                Require(owned != IntPtr.Zero);
                child.OutputDataReceived += delegate { };
                child.ErrorDataReceived += delegate { };
                child.BeginOutputReadLine(); child.BeginErrorReadLine();
                try
                {
                    Require(child.WaitForExit(10000));
                    Require(child.ExitCode == 0);
                }
                finally
                {
                    if (!child.HasExited)
                    {
                        child.Kill();
                        Require(child.WaitForExit(3000));
                    }
                }
            }
        }

        private static int ChildMain(string argument)
        {
            bool setup = argument == "--setup-duplicate";
            if (!setup && argument != "--duplicate" && argument != "--primary-exit") return 2;
            try
            {
                using (ClientInstance instance = new ClientInstance(setup))
                {
                    if (argument == "--primary-exit")
                    {
                        if (!instance.IsPrimary) return 3;
                        // Exit without executing this using's Dispose. The OS
                        // must release this process's real client ownership.
                        Environment.Exit(0);
                    }
                    else if (instance.IsPrimary) return 4;
                }
                return 0;
            }
            catch { return 1; } // Child output must never pollute the parent receipt.
        }

        private static int Main(string[] arguments)
        {
            if (arguments.Length == 1) return ChildMain(arguments[0]);
            Dictionary<string, object> receipt = new Dictionary<string, object> {
                { "schema_version", 1 } };
            string stage = "arguments";
            ClientInstance main = null;
            try
            {
                Require(arguments.Length == 0 && Environment.OSVersion.Platform == PlatformID.Win32NT);
                stage = "main_and_setup";
                main = new ClientInstance(false);
                Require(main.IsPrimary);
                using (ClientInstance setup = new ClientInstance(true))
                {
                    Require(setup.IsPrimary);
                    stage = "main_duplicate";
                    Child("--duplicate");
                    // Check the wrong mode first: consuming the expected
                    // auto-reset event first could hide a shared-name bug.
                    Require(!setup.Activation.WaitOne(0));
                    Require(main.Activation.WaitOne(1000));
                    receipt["main_duplicate_activated"] = true;
                    receipt["activation_before_window"] = true;

                    stage = "setup_duplicate";
                    Child("--setup-duplicate");
                    Require(!main.Activation.WaitOne(0));
                    Require(setup.Activation.WaitOne(1000));
                    receipt["setup_duplicate_activated"] = true;

                    stage = "dispose_reacquire";
                    main.Dispose(); main = null;
                    using (ClientInstance returned = new ClientInstance(false))
                        Require(returned.IsPrimary);
                    receipt["dispose_reacquired"] = true;

                    stage = "process_exit_reacquire";
                    // Setup is still held by this process. A fresh main
                    // owner in another process also proves distinct mutexes.
                    Child("--primary-exit");
                    using (ClientInstance returned = new ClientInstance(false))
                        Require(returned.IsPrimary);
                    receipt["process_exit_reacquired"] = true;
                    receipt["setup_distinct_mode"] = true;
                }
                receipt["status"] = "passed";
                Console.WriteLine(Json.Serializer.Serialize(receipt));
                return 0;
            }
            catch (Exception error)
            {
                receipt["status"] = "failed";
                receipt["stage"] = stage;
                receipt["failure_type"] = error.GetType().Name;
                Console.WriteLine(Json.Serializer.Serialize(receipt));
                return 1;
            }
            finally { if (main != null) main.Dispose(); }
        }
    }
}
