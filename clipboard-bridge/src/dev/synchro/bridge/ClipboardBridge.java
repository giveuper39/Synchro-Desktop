package dev.synchro.bridge;

import android.content.ClipData;
import android.content.ContentProviderClient;
import android.content.ContentResolver;
import android.content.Context;
import android.content.res.AssetFileDescriptor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Binder;
import android.os.CancellationSignal;
import android.os.Handler;
import android.os.IBinder;
import android.os.IInterface;
import android.os.Looper;
import android.os.Parcel;
import android.os.PersistableBundle;
import android.os.RemoteException;
import android.os.SystemClock;
import android.util.Base64;
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import org.json.JSONObject;

/** Opt-in shell clipboard bridge. Only the latest text or real image URI is exported. */
public final class ClipboardBridge {
  private static final String PACKAGE = "com.android.shell";
  private static final String ORIGIN_KEY = "dev.synchro.clipboard.origin";
  private static final String LISTENER = "android.content.IOnPrimaryClipChangedListener";
  private static final int MAX_TEXT = 16000;
  private static final int MAX_IMAGE_BYTES = 32 * 1024 * 1024;
  private static final long MAX_IMAGE_PIXELS = 64000000L;
  private static final String IMAGE_DIRECTORY =
      "/sdcard/Android/data/dev.potplayer.handoff/files/clipboard";
  private final PrintWriter output =
      new PrintWriter(new OutputStreamWriter(System.out, StandardCharsets.UTF_8), true);
  private final Handler main = new Handler(Looper.getMainLooper());
  private final Object clipboard;
  private final Class<?> api;
  private final String origin;
  private final AtomicLong generation = new AtomicLong();
  private final ThreadPoolExecutor images =
      new ThreadPoolExecutor(
          1,
          1,
          0,
          TimeUnit.MILLISECONDS,
          new ArrayBlockingQueue<>(1),
          runnable -> {
            Thread worker = new Thread(runnable, "synchro-clipboard-image");
            worker.setDaemon(true);
            return worker;
          },
          new ThreadPoolExecutor.DiscardOldestPolicy());
  private ContentResolver resolver;
  private Object listener;
  private final AtomicLong revision = new AtomicLong();
  private long lastTimestamp = -1;
  private int lastContentHash;

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
      if (clip == null || clip.getItemCount() == 0) {
        generation.incrementAndGet();
        clearImageSlot();
        return;
      }
      PersistableBundle extras = clip.getDescription().getExtras();
      if (extras != null && origin.equals(extras.getString(ORIGIN_KEY))) {
        generation.incrementAndGet();
        return;
      }
      long timestamp = clip.getDescription().getTimestamp();
      Uri uri = clip.getItemAt(0).getUri();
      if (uri != null && "content".equals(uri.getScheme()) && possibleImage(clip)) {
        int imageHash = uri.toString().hashCode();
        if (timestamp == lastTimestamp && imageHash == lastContentHash) return;
        lastTimestamp = timestamp;
        lastContentHash = imageHash;
        long currentGeneration = generation.incrementAndGet();
        images.execute(() -> exportImage(uri, currentGeneration));
        return;
      }
      CharSequence value = clip.getItemAt(0).getText();
      if (value == null || value.length() > MAX_TEXT || value.toString().indexOf('\0') >= 0) {
        generation.incrementAndGet();
        clearImageSlot();
        return;
      }
      int textHash = value.toString().hashCode();
      if (timestamp == lastTimestamp && textHash == lastContentHash) return;
      lastTimestamp = timestamp;
      lastContentHash = textHash;
      generation.incrementAndGet();
      clearImageSlot();
      emit(
          new JSONObject()
              .put("type", "clipboard")
              .put("revision", revision.incrementAndGet())
              .put("text", value.toString()));
    } catch (Exception error) {
      report(error);
    }
  }

  private static boolean possibleImage(ClipData clip) {
    android.content.ClipDescription description = clip.getDescription();
    return description.hasMimeType("image/*")
        || description.hasMimeType("application/octet-stream")
        || description.getMimeTypeCount() == 0
        || (description.getMimeTypeCount() == 1 && description.hasMimeType("text/uri-list"));
  }

  private static void clearImageSlot() {
    // Fixed files owned by this feature only. They are no longer the current clip.
    new File(IMAGE_DIRECTORY, "from-pc.png").delete();
    new File(IMAGE_DIRECTORY, "published/latest.png").delete();
  }

  private void exportImage(Uri uri, long currentGeneration) {
    if (resolver == null || currentGeneration != generation.get()) return;
    CancellationSignal cancellation = new CancellationSignal();
    Runnable cancel = cancellation::cancel;
    main.postDelayed(cancel, 10000);
    long deadline = SystemClock.elapsedRealtime() + 10000;
    try (ExternalProvider provider = new ExternalProvider(uri)) {
      // Clipboard providers may label a real photo as application/octet-stream.
      // Validate the bounded encoded image, rather than discarding its MIME hint.
      String mime;
      byte[] data;
      try (AssetFileDescriptor descriptor = provider.client.openAssetFile(uri, "r", cancellation)) {
        if (descriptor == null || descriptor.getDeclaredLength() > MAX_IMAGE_BYTES) return;
        try (InputStream input = descriptor.createInputStream();
            ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
          byte[] block = new byte[32768];
          int count;
          while ((count = input.read(block)) != -1) {
            if (currentGeneration != generation.get()
                || bytes.size() + count > MAX_IMAGE_BYTES
                || SystemClock.elapsedRealtime() > deadline) return;
            bytes.write(block, 0, count);
          }
          data = bytes.toByteArray();
        }
      }
      BitmapFactory.Options bounds = new BitmapFactory.Options();
      bounds.inJustDecodeBounds = true;
      BitmapFactory.decodeByteArray(data, 0, data.length, bounds);
      if (bounds.outWidth <= 0
          || bounds.outHeight <= 0
          || (long) bounds.outWidth * bounds.outHeight > MAX_IMAGE_PIXELS) return;
      mime = bounds.outMimeType;
      if (mime == null || !mime.startsWith("image/")) return;
      // Android can decode HEIF photos even when desktop Pillow cannot. Encode
      // those with the platform PNG codec; common PNG/JPEG/WebP remain encoded
      // until the desktop normalizes orientation and strips metadata.
      if (mime.equals("image/heif") || mime.equals("image/heic")) {
        Bitmap bitmap = BitmapFactory.decodeByteArray(data, 0, data.length);
        if (bitmap == null) return;
        try (ByteArrayOutputStream png = new ByteArrayOutputStream()) {
          if (!bitmap.compress(Bitmap.CompressFormat.PNG, 100, png) || png.size() > MAX_IMAGE_BYTES)
            return;
          data = png.toByteArray();
          mime = "image/png";
        } finally {
          bitmap.recycle();
        }
      }
      if (currentGeneration != generation.get()) return;
      emit(
          new JSONObject()
              .put("type", "image")
              .put("revision", revision.incrementAndGet())
              .put("mime", mime)
              .put("width", bounds.outWidth)
              .put("height", bounds.outHeight)
              .put("data", Base64.encodeToString(data, Base64.NO_WRAP)));
    } catch (Exception | OutOfMemoryError error) {
      report(error);
    } finally {
      main.removeCallbacks(cancel);
      cancellation.cancel();
    }
  }

  /**
   * Shell is not an ActivityManager-registered application process. Acquire its provider connection
   * through the external-provider API used by Android's own `content` shell command, then use the
   * normal client URI permission checks.
   */
  private final class ExternalProvider implements AutoCloseable {
    final ContentProviderClient client;
    private final Object manager;
    private final Class<?> managerApi;
    private final IBinder token = new Binder();
    private final String authority;

    ExternalProvider(Uri uri) throws Exception {
      authority = uri.getAuthority();
      Class<?> activityManager = Class.forName("android.app.ActivityManager");
      manager = activityManager.getMethod("getService").invoke(null);
      managerApi = Class.forName("android.app.IActivityManager");
      Object holder;
      try {
        holder =
            managerApi
                .getMethod(
                    "getContentProviderExternal",
                    String.class,
                    int.class,
                    IBinder.class,
                    String.class)
                .invoke(manager, authority, 0, token, "synchro-clipboard");
      } catch (NoSuchMethodException oldAndroid) {
        holder =
            managerApi
                .getMethod("getContentProviderExternal", String.class, int.class, IBinder.class)
                .invoke(manager, authority, 0, token);
      }
      if (holder == null) throw new IllegalStateException("Image provider is unavailable");
      try {
        Object remote = holder.getClass().getField("provider").get(holder);
        Class<?> providerApi = Class.forName("android.content.IContentProvider");
        Constructor<ContentProviderClient> factory =
            ContentProviderClient.class.getDeclaredConstructor(
                ContentResolver.class, providerApi, boolean.class);
        factory.setAccessible(true);
        client = factory.newInstance(resolver, remote, false);
      } catch (Exception failure) {
        release();
        throw failure;
      }
    }

    private void release() throws Exception {
      try {
        managerApi
            .getMethod(
                "removeContentProviderExternalAsUser", String.class, IBinder.class, int.class)
            .invoke(manager, authority, token, 0);
      } catch (NoSuchMethodException oldAndroid) {
        managerApi
            .getMethod("removeContentProviderExternal", String.class, IBinder.class)
            .invoke(manager, authority, token);
      }
    }

    @Override
    public void close() throws Exception {
      try {
        client.close();
      } finally {
        release();
      }
    }
  }

  private void report(Throwable error) {
    try {
      // No exception messages: OEM errors can include clipboard content.
      emit(new JSONObject().put("type", "error").put("reason", error.getClass().getSimpleName()));
    } catch (Exception ignored) {
    }
  }

  private void start() throws Exception {
    try {
      Class<?> threadApi = Class.forName("android.app.ActivityThread");
      Object thread = threadApi.getMethod("systemMain").invoke(null);
      Context system = (Context) threadApi.getMethod("getSystemContext").invoke(thread);
      Context packageContext = system.createPackageContext(PACKAGE, 0);
      Class<?> contextApi = Class.forName("android.app.ContextImpl");
      Field packageInfo = contextApi.getDeclaredField("mPackageInfo");
      packageInfo.setAccessible(true);
      Method appContext =
          contextApi.getDeclaredMethod(
              "createAppContext", threadApi, Class.forName("android.app.LoadedApk"));
      appContext.setAccessible(true);
      // A context derived directly from the system context inherits "android"
      // as its base/op package. Providers correctly reject that identity for
      // this shell UID. Create an independent application context for the real
      // shell package instead; normal clipboard URI grants still apply.
      Context shell = (Context) appContext.invoke(null, thread, packageInfo.get(packageContext));
      resolver = shell.getContentResolver();
    } catch (Exception unavailable) {
      // Older OEM builds may not expose a shell ContentResolver. Text sharing
      // remains usable and the ready handshake declares image capability.
      resolver = null;
    }
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
              if (method.getName().equals("toString")) return "SynchroClipboardListener";
              return null;
            });
    callback.attachInterface((IInterface) listener, LISTENER);
    call("addPrimaryClipChangedListener", listener);
    // Read permission is verified without replaying the pre-existing clipboard.
    call("getPrimaryClip", null);
    emit(
        new JSONObject()
            .put("type", "ready")
            .put("sdk", android.os.Build.VERSION.SDK_INT)
            .put("images", resolver != null));
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
                  ClipData clip = ClipData.newPlainText("Synchro", text);
                  PersistableBundle extras = new PersistableBundle();
                  extras.putString(ORIGIN_KEY, origin);
                  extras.putBoolean("android.content.extra.IS_SENSITIVE", true);
                  clip.getDescription().setExtras(extras);
                  generation.incrementAndGet();
                  call("setPrimaryClip", clip);
                  clearImageSlot();
                  emit(new JSONObject().put("type", "applied"));
                }
              } catch (Exception error) {
                report(error);
              } finally {
                System.exit(0); // Closing/disconnecting ADB also stops the listener.
              }
            },
            "synchro-clipboard-input");
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
      System.err.println("Synchro clipboard unavailable: " + error.getClass().getSimpleName());
      System.exit(1);
    }
  }
}
