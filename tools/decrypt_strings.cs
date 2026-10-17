// Dumps the ConfuserEx-encrypted string constants of Zalman_OZ_Wide.exe (OZ-Wide 3.1).
// Only needed to re-derive the protocol (see docs/REVERSE_ENGINEERING.md); the driver does not use it.
//
// Runs on Windows with .NET Framework. The obfuscator's module initializer only works there:
//   C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe -platform:x86 -out:decrypt_strings.exe decrypt_strings.cs
//   decrypt_strings.exe Zalman_OZ_Wide.exe keys.txt strings_b64.txt
// keys.txt   : one int per line, the arguments of every `<Module>.X<string>(key)` call in the decompiled source
// output     : "<key>\t<decrypter index>\t<base64 of UTF-8 string>" per decrypted string
//
// The decrypters return null unless called from code inside the app's own assembly, so each one is
// wrapped in a DynamicMethod owned by that module.
using System; using System.IO; using System.Linq; using System.Collections.Generic;
using System.Reflection; using System.Reflection.Emit; using System.Runtime.CompilerServices;
class Dec {
    static void Main(string[] a) {
        var asm = Assembly.LoadFrom(a[0]);
        var mod = asm.ManifestModule;
        RuntimeHelpers.RunModuleConstructor(mod.ModuleHandle);
        var f = BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;
        var ds = mod.GetMethods(f)
            .Where(m => m.IsGenericMethodDefinition && m.GetParameters().Length == 1
                     && m.GetParameters()[0].ParameterType == typeof(int))
            .Select(m => m.MakeGenericMethod(typeof(string))).ToList();
        var fns = new List<Func<int,string>>();
        foreach (var m in ds) {
            var dm = new DynamicMethod("d", typeof(string), new[]{typeof(int)}, mod, true);
            var il = dm.GetILGenerator();
            il.Emit(OpCodes.Ldarg_0); il.Emit(OpCodes.Call, m); il.Emit(OpCodes.Ret);
            fns.Add((Func<int,string>)dm.CreateDelegate(typeof(Func<int,string>)));
        }
        var w = new StreamWriter(a[2]);
        foreach (var line in File.ReadLines(a[1])) {
            int key; if (!int.TryParse(line.Trim(), out key)) continue;
            for (int i = 0; i < fns.Count; i++) {
                try {
                    var s = fns[i](key);
                    if (s == null) continue;
                    w.WriteLine(key + "\t" + i + "\t" + Convert.ToBase64String(System.Text.Encoding.UTF8.GetBytes(s)));
                } catch { }
            }
        }
        w.Close();
    }
}
