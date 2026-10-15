package dev.syncro.bridge;

import android.content.ClipData;
import android.os.Binder;
import android.os.Handler;
import android.os.IBinder;
import android.os.IInterface;
import android.os.Looper;
import android.os.Parcel;
import android.os.PersistableBundle;
import android.os.RemoteException;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

/** An opt-in ADB shell process. Clipboard text travels only through its private stdio pipe. */
public final class ClipboardBridge {
  private static final String PACKAGE = "com.android.shell";
  private static final String ORIGIN_KEY = "dev.syncro.clipboard.origin";
  private static final String LISTENER = "android.content.IOnPrimaryClipChangedListener";
  private static final int MAX_TEXT = 16000;
  private final PrintWriter output =
      new PrintWriter(new OutputStreamWriter(System.out, StandardCharsets.UTF_8), true);
  private final Handler main = new Handler(Looper.getMainLooper());
  private final Object clipboard;
  private final Class<?> api;
  private final String origin;
  private Object listener;
  private long revision;
  private long lastTimestamp = -1;
  private int lastTextHash;

  private ClipboardBridge(String origin) throws Exception {
    this.origin = origin;
    Class<?> services = Class.forName("android.os.ServiceManager");
    IBinder binder =
        (IBinder) services.getMethod("getService", String.class).invoke(null, "clipboard");
    if (binder == null) throw new IllegalStateException("Clipboard service is unavailable");
    api = Class.forName("android.content.IClipboard");
    clipboard =
        Class.forName("android.content.IClipboard$Stub")
            .getMethod("asInterface", IBinder.class)
            .invoke(null, binder);
  }

  /** New Android versions append attribution/device fields. Unknown field types fail closed. */
  private Object call(String name, Object value) throws Exception {
    Method method = null;
    for (Method candidate : api.getMethods())
      if (candidate.getName().equals(name)) {
        method = candidate;
        break;
      }
    if (method == null) throw new NoSuchMethodException(name);
    Class<?>[] types = method.getParameterTypes();
    Object[] args = new Object[types.length];
    boolean packageSet = false;
    for (int i = 0; i < types.length; i++) {
      if (types[i] == String.class) {
        args[i] = packageSet ? null : PACKAGE;
        packageSet = true;
      } else if (types[i] == int.class) {
        args[i] = 0; // Primary Android user and default display device.
      } else if (types[i] == boolean.class) {
        args[i] = false;
      } else if (value != null && types[i].isInstance(value)) {
        args[i] = value;
      } else {
        throw new IllegalStateException("Unsupported clipboard API field");
      }
    }
    return method.invoke(clipboard, args);
  }

  private synchronized void emit(JSONObject message) {
    output.println(message.toString());
  }

  private void changed() {
    try {
      ClipData clip = (ClipData) call("getPrimaryClip", null);
      if (clip == null || clip.getItemCount() == 0) return;
      PersistableBundle extras = clip.getDescription().getExtras();
      if (extras != null && origin.equals(extras.getString(ORIGIN_KEY))) return;
      CharSequence value = clip.getItemAt(0).getText();
      if (value == null || value.length() > MAX_TEXT) return;
      long timestamp = clip.getDescription().getTimestamp();
      int textHash = value.toString().hashCode();
      if (timestamp == lastTimestamp && textHash == lastTextHash) return;
      lastTimestamp = timestamp;
      lastTextHash = textHash;
      emit(
          new JSONObject()
              .put("type", "clipboard")
              .put("revision", ++revision)
              .put("text", value.toString()));
    } catch (Exception error) {
      report(error);
    }
  }

  private void report(Exception error) {
    try {
      // No exception messages: OEM errors can include clipboard content.
      emit(new JSONObject().put("type", "error").put("reason", error.getClass().getSimpleName()));
    } catch (Exception ignored) {
    }
  }

  private void start() throws Exception {
    Binder callback =
        new Binder() {
          @Override
          protected boolean onTransact(int code, Parcel data, Parcel reply, int flags)
              throws RemoteException {
            if (code == IBinder.FIRST_CALL_TRANSACTION) {
              data.enforceInterface(LISTENER);
              main.post(ClipboardBridge.this::changed);
              return true;
            }
            return super.onTransact(code, data, reply, flags);
          }
        };
    Class<?> listenerType = Class.forName(LISTENER);
    listener =
        Proxy.newProxyInstance(
            listenerType.getClassLoader(),
            new Class<?>[] {listenerType},
            (proxy, method, args) -> {
              if (method.getName().equals("asBinder")) return callback;
              if (method.getName().equals("toString")) return "SyncroClipboardListener";
              return null;
            });
    callback.attachInterface((IInterface) listener, LISTENER);
    call("addPrimaryClipChangedListener", listener);
    // Read permission is verified without replaying the pre-existing clipboard.
    call("getPrimaryClip", null);
    emit(new JSONObject().put("type", "ready").put("sdk", android.os.Build.VERSION.SDK_INT));
    Thread input =
        new Thread(
            () -> {
              try (BufferedReader reader =
                  new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) {
                  if (line.length() > 100000)
                    throw new IllegalArgumentException("Oversized message");
                  JSONObject request = new JSONObject(line);
                  String text = request.getString("text");
                  if (text.length() > MAX_TEXT || text.indexOf('\0') >= 0)
                    throw new IllegalArgumentException("Invalid text");
                  ClipData clip = ClipData.newPlainText("Синхро", text);
                  PersistableBundle extras = new PersistableBundle();
                  extras.putString(ORIGIN_KEY, origin);
                  extras.putBoolean("android.content.extra.IS_SENSITIVE", true);
                  clip.getDescription().setExtras(extras);
                  call("setPrimaryClip", clip);
                  emit(new JSONObject().put("type", "applied"));
                }
              } catch (Exception error) {
                report(error);
              } finally {
                System.exit(0); // Closing/disconnecting ADB also stops the listener.
              }
            },
            "syncro-clipboard-input");
    input.start();
  }

  public static void main(String[] args) {
    try {
      if (args.length != 1 || !args[0].matches("[a-f0-9]{32}"))
        throw new IllegalArgumentException("Origin is required");
      Looper.prepareMainLooper();
      ClipboardBridge bridge = new ClipboardBridge(args[0]);
      bridge.start();
      Looper.loop();
    } catch (Exception error) {
      // The controller receives only a class name, never a clipboard value.
      System.err.println("Syncro clipboard unavailable: " + error.getClass().getSimpleName());
      System.exit(1);
    }
  }
}
