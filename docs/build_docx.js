// Builds the design document from the same content as design.md.
//   node build_docx.js
// Then: soffice --headless --convert-to pdf Data-Synchronisation-Utility.docx

const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, AlignmentType,
  BorderStyle, LevelFormat, ImageRun,
} = require("docx");

const DIR = "/root/resumable-file-sync/docs";

const INK = "1F2933";
const ACCENT = "1F3A5F";
const MUTED = "4A5568";
const BODY = 19;          // half-points, so 9.5pt
const FONT = "Calibri";

const t = (text, opts = {}) => new TextRun({ text, font: FONT, size: BODY, color: INK, ...opts });
const b = (text) => t(text, { bold: true });
const mono = (text) => new TextRun({ text, font: "Consolas", size: BODY - 2, color: ACCENT });

const p = (children, opts = {}) => new Paragraph({
  spacing: { after: 90, line: 240 }, ...opts, children,
});

const h = (text) => new Paragraph({
  keepNext: true,                 // a heading never sits alone at the foot of a page
  spacing: { before: 130, after: 55 },
  children: [new TextRun({ text, font: FONT, size: 23, bold: true, color: ACCENT })],
});

const bullet = (children) => new Paragraph({
  numbering: { reference: "dots", level: 0 },
  spacing: { after: 32, line: 232 },
  children,
});

const codeLine = (text) => new Paragraph({
  spacing: { after: 0, line: 205 },
  indent: { left: 200 },
  children: [new TextRun({ text, font: "Consolas", size: 16, color: ACCENT })],
});

// Reads the pixel size out of the PNG header, so the aspect ratio is always right.
function pngSize(file) {
  const buf = fs.readFileSync(file);
  return { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) };
}

function image(file, width) {
  const full = `${DIR}/${file}`;
  const { w, h: ph } = pngSize(full);
  return new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 45, after: 70 },
    children: [new ImageRun({
      type: "png",
      data: fs.readFileSync(full),
      transformation: { width, height: Math.round((width * ph) / w) },
    })],
  });
}

const doc = new Document({
  creator: "Matthew Mikhail",
  title: "Data Synchronisation Utility",
  numbering: {
    config: [{
      reference: "dots",
      levels: [{
        level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 250, hanging: 170 } } },
      }],
    }],
  },
  sections: [{
    properties: { page: { margin: { top: 700, right: 780, bottom: 640, left: 780 } } },
    children: [
      new Paragraph({
        spacing: { after: 20 },
        children: [new TextRun({
          text: "Data Synchronisation Utility",
          font: FONT, size: 32, bold: true, color: ACCENT,
        })],
      }),
      new Paragraph({
        spacing: { after: 160 },
        border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "C5CED6", space: 6 } },
        children: [new TextRun({
          text: "Design document.  Matthew Mikhail.",
          font: FONT, size: 18, color: MUTED,
        })],
      }),

      h("Summary"),
      p([t("An agent runs on the client machine, watches a set of folders, and sends anything new or changed to a collection server. It only sends what the server does not already have, and it sends it in small pieces so a dropped connection can carry on instead of starting again. The server checks every file against a fingerprint before it saves it.")]),

      h("What was asked for"),
      bullet([t("Only send files that have changed since the last sync.")]),
      bullet([t("Use as little bandwidth as possible.")]),
      bullet([t("Handle a bad connection, without restarting a transfer from scratch.")]),
      bullet([t("Let the server confirm the file it received is identical to the one on the client.")]),

      h("High level design"),
      image("high-level.png", 520),
      p([t("The agent has three jobs: notice a file changed, work out whether it is worth sending, and send it.")]),
      p([t("It spots changes two ways. The operating system can tell it when a file is written, which is quick but not always reliable, so the agent also walks the folders on a timer and checks each file's size and modified time against what it saw last time. The timed scan is the one I trust; the events just make it faster.")]),
      p([t("When a file looks changed, the agent takes a SHA-256 hash of it, and that hash is the file's identity from then on. It asks the server whether it already has that hash. If it does, nothing is sent, which is what makes a rename or a copy almost free. If it does not, the file goes up in 1 MB pieces.")]),

      h("How it works"),
      bullet([t("Only files whose size or modified time changed get hashed. Hashing everything on every scan would work, but it means reading every file every few minutes.")]),
      bullet([t("The client asks about a batch of files in one request. The server answers each one: skip it, I already have that content, or send it from byte N.")]),
      bullet([t("The file goes up in 1 MB chunks, each with its own hash, so a piece that arrives corrupted costs one chunk rather than the file.")]),
      bullet([t("The server keeps track of how much it has, and the client asks rather than assuming. After a dropped connection the client picks up from the server's number.")]),
      bullet([t("If a request fails the client waits and tries again, a few times, waiting a bit longer each time. If the file still will not go, it stops and reports it with the reason rather than retrying forever.")]),
      bullet([t("At the end the server hashes everything it received and compares it against the hash the client gave at the start, and only saves the file if they match. So if someone edits the file while it is being sent, the server ends up with a mix of old and new bytes, that will not match, and nothing gets saved. The client sends the new version next time round.")]),
      bullet([t("Paths from the client are checked and rejected if they look wrong, and are never used to build a path on the server's disk. Clients can only write in their own area, and a delete is recorded rather than actually carried out.")]),

      h("The message flow"),
      image("low-level.png", 510),

      h("Code"),
      p([t("The loop that sends the file. It is short, and it is the part I would want someone to review:")],
        { spacing: { after: 70, line: 240 } }),
      codeLine('offset = plan["offset"]         # how much the server already has'),
      codeLine('with open(local_path, "rb") as fh:'),
      codeLine("    while True:"),
      codeLine("        fh.seek(offset)"),
      codeLine("        chunk = fh.read(chunk_size)"),
      codeLine("        if not chunk:"),
      codeLine("            break"),
      codeLine("        offset = self._put_chunk(upload_id, offset, chunk)"),
      p([t("I picked this bit because it is where things go wrong if you assume too much. The loop does not keep track of its own position. It moves to whatever byte the server last reported, so resuming after a drop and starting fresh are the same few lines. The retries sit underneath it in "), mono("request()"), t(".")],
        { spacing: { before: 110, after: 90, line: 240 } }),

      h("Another way it could be done"),
      p([t("The alternative is an rsync style approach: break the file into blocks, compare them with the server, and send only the blocks that differ. It would send far less for a large file with a small edit, which is where my design is weakest. I did not go that way for a first version because the server then has to keep an index of every block of every file, and that is a lot more to build and to get right. If the files turned out to be large with small changes, I would look at it again.")]),

      h("Pros and cons"),
      p([b("Pros")], { spacing: { after: 40, line: 230 } }),
      bullet([t("Renames and copies cost almost nothing.")]),
      bullet([t("A dropped connection costs one chunk, not the whole file.")]),
      bullet([t("The server can prove that what it saved is what the client read.")]),
      bullet([t("No dependencies, and one small database file on the client.")]),
      p([b("Cons")], { spacing: { before: 80, after: 40, line: 230 } }),
      bullet([t("A one byte change in a 1 GB file still sends 1 GB.")]),
      bullet([t("A large file has to be hashed before anything is sent, so there is a wait before the first byte moves.")]),
      bullet([t("A file edited back to the same size with its timestamp put back would be missed until something hashes it again.")]),
      bullet([t("Old content builds up on the server. I have not worked out how it gets cleaned up.")]),

      h("Future state"),
      bullet([t("Send a list of chunk hashes in the first request so the server can say which pieces it is missing. For a log file that keeps growing, that turns a 5 GB transfer into just the end of the file.")]),
      bullet([t("Handle deletes properly, and clean up partial uploads that were started and abandoned.")]),

      new Paragraph({
        spacing: { before: 130 },
        border: { top: { style: BorderStyle.SINGLE, size: 6, color: "C5CED6", space: 6 } },
        children: [new TextRun({
          text: "Based on SHA-256 (NIST FIPS 180-4), RFC 9110 and RFC 9530 for the HTTP behaviour, and the SQLite documentation on atomic commits. The chunk size and the retry timings are starting points and would need tuning against a real link.",
          font: FONT, size: 16, color: MUTED, italics: true,
        })],
      }),
    ],
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(`${DIR}/Data-Synchronisation-Utility.docx`, buf);
  console.log("docx written");
});
