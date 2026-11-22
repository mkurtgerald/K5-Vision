// Native controls-only QA. Never invokes bootstrap Main, product, service or auth.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class BootstrapControlsCheck
{
    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")]
    private static extern IntPtr SetWindowLongPtr(IntPtr window, int index, IntPtr value);
    private static int checks;
    private static Type helper;
    private static object Call(string name, params object[] values)
    { return helper.GetMethod(name, BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, values); }
    private static void Require(bool value) { if (!value) throw new InvalidOperationException(); checks++; }
    private static void Identify(Control control, int id) { SetWindowLongPtr(control.Handle, -12, new IntPtr(id)); }

    [STAThread]
    private static int Main(string[] arguments)
    {
        if (arguments.Length != 1) return 2;
        helper = Assembly.LoadFile(arguments[0]).GetType("WindowsGuestBootstrap", true);
        using (Form form = new Form())
        using (Button quit = new Button())
        using (Label status = new Label())
        using (TextBox diagnostic = new TextBox())
        {
            form.Text = "K5 native controls fixture";
            quit.Text = "Close"; status.Text = "Connecting";
            form.Controls.Add(quit); form.Controls.Add(status); form.Controls.Add(diagnostic);
            Identify(quit, 1005); Identify(status, 1006); Identify(diagnostic, 1010);
            status.Top = 40; diagnostic.Visible = false; form.Show(); Application.DoEvents();
            int pid = Process.GetCurrentProcess().Id;
            Require((IntPtr)Call("Form", pid) == form.Handle); // No first-admin controls yet.
            Require((IntPtr)Call("Form", Int32.MaxValue) == IntPtr.Zero);
            status.Visible = false; Application.DoEvents();
            Require((IntPtr)Call("Form", pid) == IntPtr.Zero);
            status.Visible = true; Application.DoEvents();
            Require(!(bool)Call("AdministratorControlsReady", form.Handle));
            Dictionary<int, Control> inputs = new Dictionary<int, Control>();
            foreach (int id in new[] { 1008, 1001, 1002, 1003 })
            {
                Control control = id == 1003 ? (Control)new Button() : new TextBox();
                control.Top = 70 + inputs.Count * 30; form.Controls.Add(control); Identify(control, id);
                inputs[id] = control;
            }
            Application.DoEvents(); Require((bool)Call("AdministratorControlsReady", form.Handle));
            foreach (int id in new[] { 1008, 1001, 1002, 1003 })
            {
                Control control = inputs[id];
                control.Visible = false; Application.DoEvents();
                Require(!(bool)Call("AdministratorControlsReady", form.Handle)); control.Visible = true;
                control.Enabled = false; Application.DoEvents();
                Require(!(bool)Call("AdministratorControlsReady", form.Handle)); control.Enabled = true;
                form.Controls.Remove(control); Application.DoEvents();
                Require(!(bool)Call("AdministratorControlsReady", form.Handle));
                form.Controls.Add(control); Identify(control, id); Application.DoEvents();
            }
            Require((bool)Call("AdministratorControlsReady", form.Handle));
            bool expired = false;
            try { Call("Wait", new Func<bool>(delegate { return false; }), 1); }
            catch (TargetInvocationException error) { expired = error.InnerException is TimeoutException; }
            Require(expired); // Scaled wait does not call SetText/Click or product Main.
            const string sentinel = "SECRET-QA-ONLY-DO-NOT-EXPORT";
            ((TextBox)inputs[1001]).UseSystemPasswordChar = true; inputs[1001].Text = sentinel;
            ((TextBox)inputs[1002]).UseSystemPasswordChar = true; inputs[1002].Text = sentinel;
            diagnostic.Text = new JavaScriptSerializer().Serialize(new Dictionary<string, object> {
                { "schema_version", 1 }, { "phase", "account_required" }, { "ready", false },
                { "password", sentinel }, { "recovery_codes", sentinel }, { "seed", sentinel },
                { "source_uri", sentinel }, { "busy", sentinel } });
            Call("CaptureGuiFailure", Process.GetCurrentProcess());
            Dictionary<string, object> receipt = (Dictionary<string, object>)helper.GetField("Receipt",
                BindingFlags.Static | BindingFlags.NonPublic).GetValue(null);
            string serialized = new JavaScriptSerializer().Serialize(receipt);
            Require(!serialized.Contains(sentinel) && !receipt.ContainsKey("busy"));
            Require((string)receipt["gui_status_phase"] == "account_required" &&
                receipt["gui_status_ready"] is bool && !(bool)receipt["gui_status_ready"]);
            diagnostic.Text = "{\"schema_version\":1,\"phase\":\"" + sentinel + "\",\"ready\":\"" + sentinel + "\"}";
            Call("CaptureGuiFailure", Process.GetCurrentProcess());
            Require((string)receipt["gui_status_phase"] == "other" &&
                !new JavaScriptSerializer().Serialize(receipt).Contains(sentinel));
            form.Close();
        }
        Console.WriteLine("Native bootstrap main/admin controls and diagnostic privacy passed: " + checks);
        return 0;
    }
}
