using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using DocumentFormat.OpenXml;
using DocumentFormat.OpenXml.Packaging;
using DocumentFormat.OpenXml.Validation;
using W = DocumentFormat.OpenXml.Wordprocessing;
using DW = DocumentFormat.OpenXml.Drawing.Wordprocessing;

// Private host/worker protocol. Absolute input/output paths are host-owned snapshots;
// these paths are never arguments in a model-facing tool schema.
static class Program
{
    static string Hash(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
    static string HashElement(OpenXmlElement e) => Hash(Encoding.UTF8.GetBytes(e.OuterXml));
    static string Text(OpenXmlElement e) => e switch {
        W.Drawing drawing=>drawing.Descendants<DW.DocProperties>().FirstOrDefault()?.Description?.Value ?? "",
        W.Style style=>style.StyleName?.Val?.Value ?? style.StyleId?.Value ?? "",
        W.NumberingInstance numbering=>Lists.Preview(numbering),
        W.Header or W.Footer=>string.Join("\n",e.ChildElements.Select(Text)),
        W.Table table=>string.Join("\n",table.Elements<W.TableRow>().Select(Text)),
        W.TableRow row=>string.Join("\t",row.Elements<W.TableCell>().Select(Text)),
        W.TableCell cell=>string.Join("\n",cell.ChildElements.Where(c=>c is W.Paragraph || c is W.Table).Select(Text)),
        _=>string.Concat(e.Descendants().Select(n=>n switch { W.Text t=>t.Text,W.Break=>"\n",W.TabChar=>"\t",_=>"" })) };
    static string Clip(string s, int offset, int count) => string.Concat(s.EnumerateRunes().Skip(offset).Take(count).Select(r => r.ToString()));
    static int Length(string s) => s.EnumerateRunes().Count();
    static string Str(JsonElement e, string key) => e.GetProperty(key).GetString() ?? throw new InvalidDataException("Missing string: " + key);
    static int Int(JsonElement e, string key, int fallback) => e.TryGetProperty(key, out var v) ? v.GetInt32() : fallback;
    static void Require(bool condition, string message) { if (!condition) throw new InvalidDataException(message); }

    static Dictionary<string, byte[]> Package(byte[] bytes)
    {
        using var zip = new ZipArchive(new MemoryStream(bytes), ZipArchiveMode.Read);
        Require(zip.Entries.Count <= 10000, "Package entry limit exceeded");
        var parts = new Dictionary<string, byte[]>(); long total = 0;
        foreach (var entry in zip.Entries)
        {
            total += entry.Length;
            Require(total <= 200_000_000 && entry.Length <= 50_000_000, "Expanded package limit exceeded");
            Require(!parts.ContainsKey(entry.FullName), "Duplicate package entry");
            using var stream = entry.Open(); using var copy = new MemoryStream(); stream.CopyTo(copy);
            parts.Add(entry.FullName, copy.ToArray());
        }
        return parts;
    }

    static byte[] Pack(Dictionary<string, byte[]> parts)
    {
        using var output = new MemoryStream();
        using (var zip = new ZipArchive(output, ZipArchiveMode.Create, true))
            foreach (var (name, bytes) in parts)
            {
                var entry = zip.CreateEntry(name); using var stream = entry.Open(); stream.Write(bytes);
            }
        return output.ToArray();
    }

    static Dictionary<string, OpenXmlElement> Index(OpenXmlElement document)
    {
        var map = new Dictionary<string, OpenXmlElement>();
        var counts = new Dictionary<string, int>();
        foreach (var e in document.Descendants())
        {
            var kind = e switch { W.Drawing d when d.Descendants<DocumentFormat.OpenXml.Drawing.Pictures.Picture>().Any()=>"image", W.Paragraph => "paragraph", W.Table => "table", W.TableRow=>"row", W.TableCell => "cell", W.SectionProperties => "section", _ => null };
            if (kind == null) continue;
            counts[kind] = counts.GetValueOrDefault(kind) + 1;
            map.Add(kind + ":" + counts[kind], e);
        }
        return map;
    }

    static void Replace(W.Paragraph paragraph, JsonElement operation)
    {
        if(FieldLiteralText.Handles(paragraph)){FieldLiteralText.Apply(paragraph,operation);return;}
        // Refuse text across fields, hyperlinks, drawings, tabs, controls and tracked
        // changes until their logical offset/preservation semantics are implemented.
        bool seenRun=false;
        foreach(var child in paragraph.ChildElements)
        {
            if(child is W.Run){seenRun=true;continue;}
            if(child is W.ParagraphProperties)continue;
            bool bookmarkStart=child is W.BookmarkStart b && b.Name?.Value?.StartsWith("_LocalOfficeToc")==true &&
                b.NextSibling() is W.BookmarkEnd bookmarkEnd && bookmarkEnd.Id?.Value==b.Id?.Value;
            bool finish=child is W.BookmarkEnd e && e.PreviousSibling() is W.BookmarkStart begin &&
                begin.Name?.Value?.StartsWith("_LocalOfficeToc")==true && begin.Id?.Value==e.Id?.Value;
            Require(!seenRun && (bookmarkStart||finish),"Unsupported paragraph text structure");
        }
        var runs = paragraph.Elements<W.Run>().ToList();
        Require(runs.All(r => r.ChildElements.All(e => e is W.RunProperties || e is W.Text)), "Unsupported run text structure");
        var nodes = paragraph.Descendants<W.Text>().ToList();
        int start = Int(operation, "start", -1), end = Int(operation, "end", -1);
        var replacement = Str(operation, "text");
        Require(start >= 0 && end >= start && end <= Length(Text(paragraph)), "Invalid text span");
        Require(Length(replacement) <= 8000 && !replacement.Contains('\0') && !replacement.Contains('\n') && !replacement.Contains('\r'), "Use structural tools for paragraph breaks");
        int position = 0; bool inserted = false;
        foreach (var node in nodes)
        {
            var original = node.Text; int length = Length(original), finish = position + length;
            bool touches = start == end ? (!inserted && start >= position && start <= finish) : finish > start && position < end;
            if (touches)
            {
                int left = Math.Clamp(start - position, 0, length), right = Math.Clamp(end - position, 0, length);
                var updated = Clip(original, 0, left) + (inserted ? "" : replacement) + Clip(original, right, length);
                if (updated != original) { node.Text = updated; node.Space = SpaceProcessingModeValues.Preserve; }
                inserted = true;
            }
            position = finish;
        }
        if (!inserted && start == 0 && end == 0 && nodes.Count == 0)
        {
            paragraph.AppendChild(new W.Run(new W.Text(replacement) { Space = SpaceProcessingModeValues.Preserve })); inserted = true;
        }
        Require(inserted, "No editable span found");
    }

    static object Execute(JsonElement request)
    {
        var action = Str(request, "action");
        if(action=="create")
        {
            var created=Creation.Build(request.GetProperty("arguments"));
            using(var output=new FileStream(Str(request,"output"),FileMode.CreateNew))output.Write(created);
            return new { candidate_revision=Hash(created), results=Array.Empty<object>(), created=true };
        }
        var input = Str(request, "input");
        Require(new FileInfo(input).Length <= 50_000_000, "Document file limit exceeded");
        var bytes = File.ReadAllBytes(input); var revision = Hash(bytes);
        Require(revision == Str(request, "revision"), "stale_revision");
        var parts = Package(bytes);
        var originalParts=parts.ToDictionary(kv=>kv.Key,kv=>Hash(kv.Value));
        using var package = WordprocessingDocument.Open(new MemoryStream(bytes), false);
        var main = package.MainDocumentPart ?? throw new InvalidDataException("Missing main document");
        var document = (W.Document)(main.Document ?? throw new InvalidDataException("Missing document root")).CloneNode(true);
        var styles = (W.Styles?)main.StyleDefinitionsPart?.Styles?.CloneNode(true);
        var documentBefore=HashElement(document);
        var stylesBefore=styles==null?null:HashElement(styles);
        var listDefinitions=main.NumberingDefinitionsPart?.Numbering;
        var listsBefore=listDefinitions==null?null:HashElement(listDefinitions);
        if(action=="refresh_fields")
        {
            Require(new FileInfo(Str(request,"field_snapshot")).Length<=50_000_000,"Field snapshot exceeds file limit");
            var snapshot=File.ReadAllBytes(Str(request,"field_snapshot"));Package(snapshot);
            Dictionary<string,OpenXmlPartRootElement> refreshedStories;int pageFields;
            bool toc=PageFields.HasToc(document);
            using(var refreshed=WordprocessingDocument.Open(new MemoryStream(snapshot),false))
            {
                if(toc)
                {
                    Require(styles!=null,"A style part is required for TOC cache transfer");
                    FieldCaches.Apply(document,styles!,refreshed);
                }
                (refreshedStories,pageFields)=PageFields.Apply(main,document,refreshed.MainDocumentPart!);
            }
            var changed=new List<string>();
            foreach(var (name,story) in refreshedStories)
            {
                using var stream=new MemoryStream();story.Save(stream);parts[name]=stream.ToArray();changed.Add(name);
            }
            if(HashElement(document)!=documentBefore)
            {
                using var stream=new MemoryStream();document.Save(stream);
                var name=main.Uri.ToString().TrimStart('/');parts[name]=stream.ToArray();changed.Add(name);
            }
            if(styles!=null && HashElement(styles)!=stylesBefore)
            {
                using var stream=new MemoryStream();styles!.Save(stream);
                var name=main.StyleDefinitionsPart!.Uri.ToString().TrimStart('/');parts[name]=stream.ToArray();changed.Add(name);
            }
            Require(parts.Values.Sum(value=>(long)value.Length)<=200_000_000,"Expanded candidate package limit exceeded");
            var refreshedBytes=changed.Count==0?bytes:Pack(parts);
            Require(refreshedBytes.Length<=50_000_000,"Candidate document file limit exceeded");
            using(var check=WordprocessingDocument.Open(new MemoryStream(refreshedBytes),false))
            {
                var error=new OpenXmlValidator(FileFormatVersions.Office2016).Validate(check).FirstOrDefault();
                Require(error==null,"Field cache validation failed: "+error?.Description);
            }
            using(var output=new FileStream(Str(request,"output"),FileMode.CreateNew))output.Write(refreshedBytes);
            return new {input_revision=revision,candidate_revision=Hash(refreshedBytes),changed_parts=changed,
                results=Array.Empty<object>(),unchanged=changed.Count==0,page_fields_updated=pageFields,toc_refreshed=toc};
        }
        var args = request.GetProperty("arguments");
        var searchScope=action=="find" && args.TryGetProperty("scope",out var requestedScope)?requestedScope.GetString():"main";
        if(action=="find")Require(new[]{"main","headers","footers","all"}.Contains(searchScope),"Unknown text search scope");
        var map = Index(document);
        var storyInfo=new Dictionary<string,object>();
        var storyChildren=new HashSet<string>();
        var storyRoots=new Dictionary<string,(OpenXmlPartRootElement root,string before)>();
        if(action is "read" or "list" or "patch" || (action=="find" && searchScope!="main"))
        {
            void AddStories(IEnumerable<OpenXmlPart> source,string kind)
            {
                int number=0;
                foreach(var part in source.DistinctBy(p=>p.Uri).OrderBy(p=>p.Uri.ToString(),StringComparer.Ordinal))
                {
                    var root=part is HeaderPart h?(OpenXmlElement?)h.Header:((FooterPart)part).Footer;
                    if(root==null)continue;
                    root=root.CloneNode(true);
                    storyRoots.Add(part.Uri.ToString().TrimStart('/'),((OpenXmlPartRootElement)root,HashElement(root)));
                    var id=kind+":"+(++number);map.Add(id,root);
                    foreach(var (localId,element) in Index(root))
                    {
                        var pieces=localId.Split(':');var childId=pieces[0]+":"+id+":"+pieces[1];
                        map.Add(childId,element);storyChildren.Add(childId);
                        storyInfo[childId]=new { root_id=id,kind,read_only=element is not W.Paragraph,
                            editable_operations=element is W.Paragraph?new[]{"replace_text","format_text","format_paragraph","set_style","set_character_style"}:Array.Empty<string>(),
                            contains_fields=element.Descendants().Any(e=>e is W.SimpleField || e is W.FieldCode),
                            interpretation="Header/footer object shared across its section uses. Only declared paragraph operations are editable; text structures may restrict edits. Field text is cached. Read root metadata for explicit section references." };
                    }
                    var relationshipIds=main.Parts.Where(p=>p.OpenXmlPart.Uri==part.Uri).Select(p=>p.RelationshipId).ToHashSet();
                    var bindings=new List<object>();
                    foreach(var section in map.Where(kv=>kv.Value is W.SectionProperties).ToList())
                        foreach(var reference in section.Value.ChildElements)
                        {
                            string? rid=null;string? type=null;
                            if(reference is W.HeaderReference hr && kind=="header") { rid=hr.Id?.Value;type=hr.Type?.InnerText; }
                            if(reference is W.FooterReference fr && kind=="footer") { rid=fr.Id?.Value;type=fr.Type?.InnerText; }
                            if(rid!=null && relationshipIds.Contains(rid))bindings.Add(new { section=section.Key,type });
                        }
                    storyInfo[id]=new { kind,part=part.Uri.ToString(),read_only=true,
                        contains_fields=root.Descendants().Any(e=>e is W.SimpleField || e is W.FieldCode),
                        explicit_section_references=bindings.Take(16),references_truncated=bindings.Count>16,
                        interpretation="Explicit references only; inherited section links are not resolved. Field text is cached, not a recalculated page value." };
                }
            }
            AddStories(main.HeaderParts,"header");AddStories(main.FooterParts,"footer");
        }
        if(action is "read" or "list")
        {
            foreach(var number in main.NumberingDefinitionsPart?.Numbering?.Elements<W.NumberingInstance>() ?? Enumerable.Empty<W.NumberingInstance>())
                map.Add("numbering:"+number.NumberID!.Value,number);
        }
        foreach(var style in styles?.Elements<W.Style>() ?? Enumerable.Empty<W.Style>())
            if(!string.IsNullOrEmpty(style.StyleId?.Value))map.Add("style:"+style.StyleId.Value,style);
        if(action=="find")
        {
            var query=Str(args,"query");int limit=Int(args,"limit",5);
            bool sensitive=args.GetProperty("case_sensitive").GetBoolean();
            Require(Length(query) is >=1 and <=200 && limit is >=1 and <=8,"Search bounds exceeded");
            var queryHash=Hash(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { scope=searchScope,sensitive,query })));
            var paragraphs=map.Where(kv=>kv.Value is W.Paragraph && (searchScope switch {
                "main"=>!storyChildren.Contains(kv.Key),"headers"=>kv.Value.Ancestors<W.Header>().Any(),
                "footers"=>kv.Value.Ancestors<W.Footer>().Any(),_=>true })).ToList();int paragraphIndex=0,position=0;
            if(args.TryGetProperty("cursor",out var cursor) && cursor.ValueKind==JsonValueKind.String)
            {
                var pieces=cursor.GetString()!.Split('/');
                Require(pieces.Length==4 && pieces[0]==revision && pieces[1]==queryHash && int.TryParse(pieces[2],out paragraphIndex) && int.TryParse(pieces[3],out position) && paragraphIndex>=0 && paragraphIndex<paragraphs.Count && position>=0,"stale_search_cursor");
                var current=Text(paragraphs[paragraphIndex].Value);
                Require(position<=current.Length && (position==current.Length || !char.IsLowSurrogate(current[position])),"Invalid search cursor position");
            }
            var matches=new List<object>();string? next=null;
            for(int i=paragraphIndex;i<paragraphs.Count;i++)
            {
                var kv=paragraphs[i];var value=Text(kv.Value);int seek=i==paragraphIndex?position:0;
                while(seek<=value.Length)
                {
                    int found=value.IndexOf(query,seek,sensitive?StringComparison.Ordinal:StringComparison.OrdinalIgnoreCase);
                    if(found<0)break;
                    if(matches.Count==limit){next=revision+"/"+queryHash+"/"+i+"/"+found;break;}
                    int start=Length(value[..found]),end=start+Length(value.Substring(found,query.Length));
                    int excerptStart=Math.Max(0,start-40),excerptLength=Math.Min(280,Length(value)-excerptStart);
                    var match=new Dictionary<string,object?> { ["id"]=kv.Key,["hash"]=HashElement(kv.Value),["start"]=start,["end"]=end,
                        ["text"]=Clip(value,excerptStart,excerptLength),["excerpt_offset"]=excerptStart,["total_chars"]=Length(value) };
                    if(storyInfo.TryGetValue(kv.Key,out var matchStory))match["story"]=matchStory;
                    matches.Add(match);
                    seek=found+(char.IsHighSurrogate(value[found])?2:1);
                }
                if(next!=null)break;
            }
            return new { revision,matches,next_cursor=next,search_scope=searchScope,scope=searchScope=="main"?"Main document paragraph text, including table cells; headers/footers are not searched":"Explicit "+searchScope+" paragraph search, including nested tables; story field text is cached" };
        }
        if (action == "list")
        {
            int limit = Int(args, "limit", 20), preview = Int(args, "preview_chars", 80);
            Require(limit is >= 1 and <= 50 && preview is >= 0 and <= 160, "List bounds exceeded");
            var items = map.AsEnumerable();
            // Main-story discovery stays compatible; stories require an explicit parent.
            if(!args.TryGetProperty("parent",out var requestedParent) || requestedParent.ValueKind!=JsonValueKind.String)
                items=items.Where(kv=>!storyChildren.Contains(kv.Key));
            if (args.TryGetProperty("kind", out var kind) && kind.ValueKind == JsonValueKind.String)
                items = items.Where(kv => kv.Key.StartsWith(kind.GetString()! + ":"));
            if (args.TryGetProperty("parent", out var parent) && parent.ValueKind == JsonValueKind.String)
            {
                Require(map.ContainsKey(parent.GetString()!),"unknown_object: Discover current object IDs with word.list_objects before reading or editing; never guess IDs.");
                var p = map[ parent.GetString()! ];
                items = items.Where(kv => kv.Value.Ancestors().Contains(p));
            }
            var filterHash=Hash(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new[]{
                kind.ValueKind==JsonValueKind.String?kind.GetString():"",
                requestedParent.ValueKind==JsonValueKind.String?requestedParent.GetString():"" })));
            int skip = 0;
            if (args.TryGetProperty("cursor", out var cursor) && cursor.ValueKind == JsonValueKind.String)
            {
                var pieces = (cursor.GetString() ?? throw new InvalidDataException("Invalid cursor")).Split('/');
                Require(pieces.Length == 3 && pieces[0] == revision && pieces[1] == filterHash && int.TryParse(pieces[2], out skip) && skip >= 0, "stale_cursor");
            }
            var page = items.Skip(skip).Take(limit + 1).ToList();
            return new { revision, objects = page.Take(limit).Select(kv => new { id = kv.Key, hash = HashElement(kv.Value), preview = Clip(Text(kv.Value), 0, preview) }),
                next_cursor = page.Count > limit ? revision + "/" + filterHash + "/" + (skip + limit) : null };
        }
        if (action == "read")
        {
            var ids = args.GetProperty("ids").EnumerateArray().Select(x => x.GetString()!).ToList();
            int remaining = Int(args, "max_chars", 4000), offset = Int(args, "offset", 0);
            Require(ids.Count is >= 1 and <= 8 && ids.Distinct().Count() == ids.Count && remaining is >= 1 and <= 8000 && offset >= 0, "Read bounds exceeded");
            var objects = new List<object>();
            foreach (var id in ids)
            {
                Require(map.ContainsKey(id),"unknown_object: Discover current object IDs with word.list_objects before reading or editing; never guess IDs.");
                var e = map[id]; var text = Text(e); var excerpt = Clip(text, offset, remaining); remaining -= Length(excerpt);
                var item=new Dictionary<string,object?> { ["id"]=id,["hash"]=HashElement(e),["text"]=excerpt,["offset"]=offset,["total_chars"]=Length(text),["next_offset"]=offset+Length(excerpt)<Length(text)?(int?)(offset+Length(excerpt)):null };
                if(storyInfo.TryGetValue(id,out var story))item["story"]=story;
                if(args.TryGetProperty("include_format",out var format) && format.GetBoolean())item["direct_format"]=Formatting.Describe(e);
                objects.Add(item);
            }
            return new { revision, objects };
        }
        if (action != "patch") throw new InvalidDataException("Unsupported worker action");
        Require(Encoding.UTF8.GetByteCount(args.GetRawText()) <= 32768, "Batch byte limit exceeded");
        var operations = args.GetProperty("operations").EnumerateArray().ToList();
        Require(operations.Count is >= 1 and <= 24, "Operation limit exceeded");
        Require(operations.Select(x => Str(x, "id")).Distinct().Count() == operations.Count && operations.Select(x => Str(x, "target")).Distinct().Count() == operations.Count, "Duplicate operation or target");
        foreach (var op in operations) {
            Require(map.ContainsKey(Str(op,"target")),"unknown_object: Discover current object IDs with word.list_objects before reading or editing; never guess IDs.");
            var targetId=Str(op,"target");
            if(storyInfo.ContainsKey(targetId))
                Require(storyChildren.Contains(targetId) && map[targetId] is W.Paragraph && new[]{"replace_text","format_text","format_paragraph","set_style","set_character_style"}.Contains(Str(op,"op")),
                    "Unsupported story edit: use declared paragraph operations; roots, structural edits and other story objects remain read-only");
            Require(HashElement(map[Str(op, "target")]) == Str(op, "expected_hash"), "stale_object");
        }
        var targets=operations.Select(op=>map[Str(op,"target")]).ToList();
        Require(!targets.Any(a=>targets.Any(b=>!ReferenceEquals(a,b) && a.Ancestors().Contains(b))),"Batch targets overlap; use separate checked batches");
        var results = new List<object>(); bool anyChange = false;
        foreach (var op in operations)
        {
            var e = map[Str(op, "target")]; var before = HashElement(e);
            if(Str(op,"op")=="replace_image")
            {
                Require(operations.Count==1,"Image replacement requires its own batch");
                Require(e is W.Drawing,"Image replacement requires an image target");
                anyChange |= Images.Replace(parts,main.Uri.ToString().TrimStart('/'),(W.Drawing)e,request.GetProperty("image"));
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Replaced picture source; retained frame/crop/alt text/caption; review appearance" });
                continue;
            }
            if(Str(op,"op")=="format_image")
            {
                Require(e is W.Drawing,"Image formatting requires an image target");
                ImageFormatting.Apply((W.Drawing)e,op.GetProperty("properties"));
                anyChange |= before!=HashElement(e);
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Updated image frame/crop/alt text; embedded bytes preserved; review layout" });
                continue;
            }
            if(Str(op,"op")=="set_image_layout")
            {
                Require(e is W.Drawing,"Image layout requires an image target");
                ImageLayout.Apply((W.Drawing)e,op.GetProperty("properties"));
                anyChange |= before!=HashElement(e);
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Updated image positioning/wrapping; media/crop/size preserved; review layout" });
                continue;
            }
            if(Str(op,"op")=="insert_image")
            {
                Require(operations.Count==1,"Image insertion requires its own batch");
                var added=Images.Insert(parts,main.Uri.ToString().TrimStart('/'),e,op,request.GetProperty("image"));
                var updated=Index(document);
                var insertedIds=updated.Where(kv=>added.Any(p=>ReferenceEquals(kv.Value,p) || kv.Value.Ancestors().Contains(p))).Select(kv=>kv.Key).ToArray();
                anyChange=true;
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Inserted inline image; review fit/pagination and refresh IDs",inserted_ids=insertedIds });
                continue;
            }
            if(Str(op,"op") is "create_paragraph_style" or "create_character_style")
            {
                Require(operations.Count==1 && e is W.Style && styles!=null,"Style creation requires one base style target in its own batch");
                var (created,added)=Styles.Create(styles!,(W.Style)e,op,character:Str(op,"op")=="create_character_style");anyChange |= added;
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary=added?"Created named style; existing styles/content unchanged":"Matching named style already exists",inserted_ids=new[]{"style:"+created.StyleId!.Value} });
                continue;
            }
            if(Str(op,"op")=="rename_style")
            {
                Require(e is W.Style && styles!=null,"Style rename requires a style target");
                Styles.Rename(styles!,(W.Style)e,op);
                anyChange |= before!=HashElement(e);
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Renamed style display name; identity, references and formatting preserved" });
                continue;
            }
            if(Str(op,"op") is "format_style" or "format_style_typography")
            {
                Require(e is W.Style,"Style definition formatting requires a style target");
                if(Str(op,"op")=="format_style_typography")
                    Require(op.GetProperty("properties").EnumerateObject().All(p=>new[]{"font","size_points","size_half_points","color","bold","italic"}.Contains(p.Name)),"Typography operation cannot change paragraph layout");
                Styles.Apply((W.Style)e,op.GetProperty("properties"),typographyOnly:Str(op,"op")=="format_style_typography");
                anyChange |= before!=HashElement(e);
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Updated named style definition; review all affected uses and pagination" });
                continue;
            }
            if(Str(op,"op")=="unmerge_cells")
            {
                Require(operations.Count==1 && e is W.TableCell,"Unmerging requires one top-left cell target in its own batch");
                var changed=TableSplitting.Apply((W.TableCell)e);anyChange |= changed;
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary=changed?"Restored separate grid cells; existing content kept in its current physical cells, new cells empty; refresh IDs and review":"Cell is already unmerged" });
                continue;
            }
            if(Str(op,"op")=="merge_cells")
            {
                Require(operations.Count==1 && e is W.Table,"Cell merging requires one table-target operation in its own batch");
                TableMerging.Apply((W.Table)e,Int(op,"row_start",-1),Int(op,"row_count",0),Int(op,"column_start",-1),Int(op,"column_count",0));
                anyChange=true;
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Merged selected rectangle; retained paragraph content in row-major order and top-left cell formatting; refresh IDs and review layout" });
                continue;
            }
            if(Str(op,"op")=="format_row")
            {
                Require(e is W.TableRow,"Row formatting requires a row target");
                RowFormatting.Apply((W.TableRow)e,op.GetProperty("properties"));
                anyChange |= before!=HashElement(e);
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Updated direct row layout; review page breaks and possible exact-height clipping" });
                continue;
            }
            if(Str(op,"op")=="format_cell")
            {
                Require(e is W.TableCell,"Cell formatting requires a cell target");
                CellFormatting.Apply((W.TableCell)e,op.GetProperty("properties"));
                anyChange |= before!=HashElement(e);
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Updated direct cell formatting; text, widths and merges preserved" });
                continue;
            }
            if(Str(op,"op")=="format_table")
            {
                Require(e is W.Table,"Table formatting requires a table target");
                TableFormatting.Apply((W.Table)e,op.GetProperty("properties"));
                anyChange |= before!=HashElement(e);
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Updated table layout; cell overrides preserved; review rendered fit" });
                continue;
            }
            if(Str(op,"op")=="set_section_layout")
            {
                Require(e is W.SectionProperties,"Page layout requires a section target");
                Sections.Apply((W.SectionProperties)e,op.GetProperty("properties"));
                anyChange |= before!=HashElement(e);
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Updated section layout; review pagination" });
                continue;
            }
            if(Str(op,"op")=="set_cell_text")
            {
                Require(e is W.TableCell,"Cell text replacement requires a cell target");
                Tables.SetText((W.TableCell)e,Str(op,"text"));
                anyChange |= before!=HashElement(e);
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Replaced cell text; refresh paragraph locators" });
                continue;
            }
            if(Str(op,"op")=="insert_rows")
            {
                Require(operations.Count==1 && e is W.TableRow,"Row insertion requires one row-target operation per batch");
                var rows=op.GetProperty("rows").EnumerateArray().ToList();Require(rows.Count is >=1 and <=8,"Row insertion limit exceeded");
                var position=Str(op,"position");Require(position=="before" || position=="after","Invalid row insertion position");
                var added=new List<W.TableRow>();
                foreach(var row in rows)
                {
                    var values=row.EnumerateArray().Select(v=>v.GetString()!).ToList();
                    Require(values.Count is >=1 and <=12 && values.All(v=>Length(v)<=8000),"Inserted cell bounds exceeded");
                    added.Add(Tables.NewRow((W.TableRow)e,values,op.GetProperty("header_row").GetBoolean()));
                }
                OpenXmlElement anchor=e;
                foreach(var row in added) { if(position=="before")e.InsertBeforeSelf(row);else { anchor.InsertAfterSelf(row);anchor=row; } }
                var updated=Index(document);var insertedIds=added.Select(row=>updated.First(kv=>ReferenceEquals(kv.Value,row)).Key).ToArray();
                anyChange=true;
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Inserted "+added.Count+" rows; refresh revision-bound IDs",inserted_ids=insertedIds });
                continue;
            }
            if(Str(op,"op")=="insert_table")
            {
                Require(operations.Count==1 && e is W.Paragraph && e.Parent is W.Body,"Table insertion requires one body-paragraph anchor in its own batch");
                var position=Str(op,"position");Require(position=="before" || position=="after","Invalid table insertion position");
                var style=Str(op,"style");
                Require(main.StyleDefinitionsPart?.Styles?.Elements<W.Style>().Any(s=>s.StyleId?.Value==style && s.Type?.Value==W.StyleValues.Paragraph)==true,"Unknown paragraph style");
                var rows=op.GetProperty("rows").EnumerateArray().Select(r=>r.EnumerateArray().Select(v=>v.GetString()!).ToList()).ToList();
                var widths=op.GetProperty("column_widths_twips").EnumerateArray().Select(v=>v.GetInt32()).ToList();
                var table=Tables.Create(rows,widths,op.GetProperty("header_row").GetBoolean(),style);
                var adjacent=position=="before"?e.PreviousSibling():e.NextSibling();
                var added=new List<OpenXmlElement> { table };
                if(position=="before")e.InsertBeforeSelf(table);else e.InsertAfterSelf(table);
                if(adjacent is W.Table)
                {
                    var separator=Creation.Paragraph("",style);
                    if(position=="before")table.InsertBeforeSelf(separator);else table.InsertAfterSelf(separator);
                    added.Add(separator);
                }
                var updated=Index(document);var insertedIds=added.Select(item=>updated.First(kv=>ReferenceEquals(kv.Value,item)).Key).ToArray();
                anyChange=true;
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),summary="Inserted table with explicit widths; refresh IDs and review pagination",inserted_ids=insertedIds });
                continue;
            }
            if(Str(op,"op")=="delete_paragraph")
            {
                Require(operations.Count==1,"Paragraph deletion requires its own batch");
                ParagraphDeletion.Apply(e,document);
                anyChange=true;
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=(string?)null,deleted=true,
                    summary="Deleted paragraph; refresh revision-bound object IDs" });
                continue;
            }
            if(Str(op,"op")=="insert_paragraphs")
            {
                Require(operations.Count==1,"Structural insertion requires its own batch");
                Require((e is W.Paragraph || e is W.Table) && (e.Parent is W.Body || e.Parent is W.TableCell),"Insertion requires a body or table-cell paragraph/table anchor");
                var position=Str(op,"position");Require(position=="before" || position=="after","Invalid insertion position");
                var blocks=op.GetProperty("paragraphs").EnumerateArray().ToList();
                Require(blocks.Count is >=1 and <=24,"Insertion block limit exceeded");
                var added=new List<W.Paragraph>();
                foreach(var block in blocks)
                {
                    var style=Str(block,"style");
                    Require(main.StyleDefinitionsPart?.Styles?.Elements<W.Style>().Any(s=>s.StyleId?.Value==style && s.Type?.Value==W.StyleValues.Paragraph)==true,"Unknown paragraph style");
                    var text=Str(block,"text");Require(Length(text)<=8000,"Inserted paragraph text limit exceeded");
                    var p=Creation.Paragraph(text,style);
                    if(block.TryGetProperty("page_break_before",out var pageBreak) && pageBreak.GetBoolean())p.ParagraphProperties!.AddChild(new W.PageBreakBefore());
                    added.Add(p);
                }
                OpenXmlElement anchor=e;
                foreach(var p in added)
                {
                    if(position=="before")e.InsertBeforeSelf(p);
                    else { anchor.InsertAfterSelf(p);anchor=p; }
                }
                var updated=Index(document);
                var insertedIds=added.Select(p=>updated.First(kv=>ReferenceEquals(kv.Value,p)).Key).ToArray();
                anyChange=true;
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),
                    summary="Inserted "+added.Count+" paragraphs; refresh revision-bound object IDs",inserted_ids=insertedIds });
                continue;
            }
            Require(e is W.Paragraph, "Initial patch operations require a paragraph target");
            var paragraph = (W.Paragraph)e;
            if(Str(op,"op")=="set_list" && op.TryGetProperty("restart_at",out var restart) && restart.ValueKind!=JsonValueKind.Null)
            {
                var numberId=Lists.Restart(paragraph,listDefinitions,Int(op,"number_id",-1),Int(op,"level",0),restart.GetInt32());
                anyChange=true;
                results.Add(new { id=Str(op,"id"),target=Str(op,"target"),before_hash=before,after_hash=HashElement(e),
                    number_id=numberId,summary="Restarted selected paragraph; continue explicitly using number_id="+numberId });
                continue;
            }
            switch (Str(op, "op"))
            {
                case "format_text":TextFormatting.Apply(paragraph,op);break;
                case "set_character_style":
                    var characterStyle=Str(op,"style_id");
                    Require(styles?.Elements<W.Style>().Any(s=>s.StyleId?.Value==characterStyle && s.Type?.Value==W.StyleValues.Character)==true,"Unknown character style");
                    TextFormatting.Apply(paragraph,op,run=>{
                        run.RunProperties ??= new W.RunProperties();
                        run.RunProperties.RunStyle=new W.RunStyle { Val=characterStyle };
                    });break;
                case "set_list":Lists.Apply(paragraph,main.NumberingDefinitionsPart?.Numbering,Int(op,"number_id",-1),Int(op,"level",0));break;
                case "format_paragraph": Formatting.Apply(paragraph,op.GetProperty("properties"));break;
                case "replace_text": Replace(paragraph, op); break;
                case "set_style":
                    var style = Str(op, "style_id");
                    Require(main.StyleDefinitionsPart?.Styles?.Elements<W.Style>().Any(s => s.StyleId?.Value == style && s.Type?.Value == W.StyleValues.Paragraph) == true, "Unknown paragraph style");
                    paragraph.ParagraphProperties ??= new W.ParagraphProperties();
                    paragraph.ParagraphProperties.ParagraphStyleId = new W.ParagraphStyleId { Val = style }; break;
                default: throw new InvalidDataException("Unsupported patch operation");
            }
            anyChange |= before != HashElement(e);
            results.Add(new { id = Str(op, "id"), target = Str(op, "target"), before_hash = before, after_hash = HashElement(e), summary = (Str(op, "op")=="replace_text" ? "Replaced text inside paragraph; paragraph remains present" : Str(op, "op")) + (storyChildren.Contains(Str(op,"target"))?"; affects all uses of the shared header/footer part":"") });
        }
        if (!anyChange)
        {
            using (var output = new FileStream(Str(request, "output"), FileMode.CreateNew)) output.Write(bytes);
            return new { input_revision = revision, candidate_revision = revision, results, changed_parts = Array.Empty<string>(), unchanged = true };
        }
        var mainName = main.Uri.ToString().TrimStart('/');
        var changedParts=new List<string>();
        if(HashElement(document)!=documentBefore)
        {
            using var output=new MemoryStream();document.Save(output);parts[mainName]=output.ToArray();changedParts.Add(mainName);
        }
        if(styles!=null && HashElement(styles)!=stylesBefore)
        {
            var name=main.StyleDefinitionsPart!.Uri.ToString().TrimStart('/');
            using var output=new MemoryStream();styles.Save(output);parts[name]=output.ToArray();changedParts.Add(name);
        }
        if(listDefinitions!=null && HashElement(listDefinitions)!=listsBefore)
        {
            var name=main.NumberingDefinitionsPart!.Uri.ToString().TrimStart('/');
            using var output=new MemoryStream();listDefinitions.Save(output);parts[name]=output.ToArray();changedParts.Add(name);
        }
        foreach(var (name,story) in storyRoots)
            if(HashElement(story.root)!=story.before)
            {
                using var output=new MemoryStream();story.root.Save(output);parts[name]=output.ToArray();changedParts.Add(name);
            }
        Require(parts.Values.Sum(value=>(long)value.Length)<=200_000_000,"Expanded candidate package limit exceeded");
        var candidate = Pack(parts);
        Require(candidate.Length<=50_000_000,"Candidate document file limit exceeded");
        using (var check = WordprocessingDocument.Open(new MemoryStream(candidate), false))
        {
            var error = new OpenXmlValidator(FileFormatVersions.Office2016).Validate(check).FirstOrDefault();
            Require(error == null, "Package validation failed: " + error?.Description);
        }
        // The host chooses a new temporary output path; never overwrite it or input.
        using (var output = new FileStream(Str(request, "output"), FileMode.CreateNew)) output.Write(candidate);
        return new { input_revision = revision, candidate_revision = Hash(candidate), results,
            changed_parts=parts.Where(kv=>!originalParts.ContainsKey(kv.Key) || Hash(kv.Value)!=originalParts[kv.Key]).Select(kv=>kv.Key).ToArray(),
            image_relationships_part=Images.Relationships(mainName) };
    }

    static int Main()
    {
        try
        {
            var buffer = new char[65537]; int length = 0, n;
            while (length < buffer.Length && (n = Console.In.Read(buffer, length, buffer.Length - length)) > 0) length += n;
            Require(length <= 65536, "Worker request limit exceeded");
            using var request = JsonDocument.Parse(new string(buffer, 0, length));
            var response = JsonSerializer.Serialize(new { ok = true, result = Execute(request.RootElement) });
            Require(Encoding.UTF8.GetByteCount(response) <= 65536, "Worker response limit exceeded");
            Console.WriteLine(response); return 0;
        }
        catch (Exception ex)
        {
            Console.WriteLine(JsonSerializer.Serialize(new { ok = false, error = ex.Message[..Math.Min(ex.Message.Length, 500)] })); return 1;
        }
    }
}
