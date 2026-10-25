export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col justify-center gap-6 px-6 py-16 sm:px-10">
      <p className="text-sm font-semibold uppercase tracking-widest text-stone-500">Media Downloader</p>
      <h1 className="text-4xl font-semibold tracking-tight text-stone-900 sm:text-5xl">
        Your media, your format.
      </h1>
      <p className="max-w-xl text-lg leading-8 text-stone-600">
        A workspace for media you own or have permission to download. Uploads and approved direct
        links are planned for later phases.
      </p>
      <p className="text-sm text-stone-500" role="status">
        Phase 1 scaffold is ready. Media processing is not available yet.
      </p>
    </main>
  );
}

