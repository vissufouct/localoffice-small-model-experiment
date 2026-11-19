using System.Text.Json;
using DocumentFormat.OpenXml;
using DocumentFormat.OpenXml.Packaging;
using DocumentFormat.OpenXml.Validation;
using W = DocumentFormat.OpenXml.Wordprocessing;

static class Creation
{
    static string Str(JsonElement e,string key,string fallback="") => e.TryGetProperty(key,out var v) ? v.GetString()! : fallback;
    static int Int(JsonElement e,string key,int fallback) => e.TryGetProperty(key,out var v) ? v.GetInt32() : fallback;
    static bool Bool(JsonElement e,string key,bool fallback=false) => e.TryGetProperty(key,out var v) ? v.GetBoolean() : fallback;
    static int FontHalfPoints(JsonElement style)
    {
        bool points=style.TryGetProperty("size_points",out var p) && p.ValueKind!=JsonValueKind.Null;
        bool halves=style.TryGetProperty("size_half_points",out var h) && h.ValueKind!=JsonValueKind.Null;
        if(points && halves)throw new InvalidDataException("Supply one font-size representation");
        if(points)
        {
            var value=p.GetDecimal();
            if(value<6 || value>144 || value*2!=decimal.Truncate(value*2))
                throw new InvalidDataException("Font size must be 6–144 points in half-point increments");
            return decimal.ToInt32(value*2);
        }
        var result=halves?h.GetInt32():22;
        if(result<12 || result>288)throw new InvalidDataException("Invalid half-point font size");
        return result;
    }

    internal static W.Paragraph Paragraph(string text,string style="Normal")
    {
        var p=new W.Paragraph(new W.ParagraphProperties(new W.ParagraphStyleId { Val=style }));
        var run=p.AppendChild(new W.Run());
        var lines=text.Replace("\r\n","\n").Replace('\r','\n').Split('\n');
        for(int i=0;i<lines.Length;i++)
        {
            if(i>0)run.AppendChild(new W.Break());
            var tabs=lines[i].Split('\t');
            for(int j=0;j<tabs.Length;j++)
            {
                if(j>0)run.AppendChild(new W.TabChar());
                run.AppendChild(new W.Text(tabs[j]) { Space=SpaceProcessingModeValues.Preserve });
            }
        }
        return p;
    }

    static W.Paragraph Band(JsonElement band)
    {
        var paragraph=Paragraph(Str(band,"text"),Str(band,"style","Normal"));
        paragraph.ParagraphProperties!.AddChild(new W.Justification { Val=Str(band,"alignment","left") switch {
            "center"=>W.JustificationValues.Center,"right"=>W.JustificationValues.Right,_=>W.JustificationValues.Left } });
        void Field(string code) => paragraph.Append(new W.SimpleField(new W.Run(new W.Text("1"))) { Instruction=" "+code+" ",Dirty=true });
        var numbering=Str(band,"page_numbers","none");
        if(numbering!="none")
        {
            Field("PAGE");
            if(numbering=="current_total")
            {
                paragraph.Append(new W.Run(new W.Text(Str(band,"separator"," / ")) { Space=SpaceProcessingModeValues.Preserve }));
                Field("NUMPAGES");
            }
        }
        return paragraph;
    }

    internal static byte[] Build(JsonElement args)
    {
        using var memory=new MemoryStream();
        using(var doc=WordprocessingDocument.Create(memory,WordprocessingDocumentType.Document,true))
        {
            var main=doc.AddMainDocumentPart();main.Document=new W.Document(new W.Body());
            var stylePart=main.AddNewPart<StyleDefinitionsPart>();stylePart.Styles=new W.Styles();
            var styles=args.GetProperty("styles").EnumerateArray().ToList();
            if(!styles.Any(s=>Str(s,"id")=="Normal"))
                stylePart.Styles.AppendChild(new W.Style(new W.StyleName { Val="Normal" },
                    new W.StyleRunProperties(new W.RunFonts { Ascii="Arial",HighAnsi="Arial" },new W.FontSize { Val="22" }))
                    { Type=W.StyleValues.Paragraph,StyleId="Normal",Default=true });
            foreach(var s in styles)
            {
                var paragraph=new W.StyleParagraphProperties();
                paragraph.Append(new W.SpacingBetweenLines { After=Int(s,"spacing_after_twips",120).ToString() });
                paragraph.Append(new W.Justification { Val=Str(s,"alignment","left") switch {"center"=>W.JustificationValues.Center,"right"=>W.JustificationValues.Right,"both"=>W.JustificationValues.Both,_=>W.JustificationValues.Left} });
                if(s.TryGetProperty("heading_level",out var level) && level.ValueKind==JsonValueKind.Number)
                    paragraph.Append(new W.OutlineLevel { Val=level.GetInt32()-1 });
                var run=new W.StyleRunProperties(new W.RunFonts { Ascii=Str(s,"font","Arial"),HighAnsi=Str(s,"font","Arial") });
                if(Bool(s,"bold"))run.Append(new W.Bold());
                if(Bool(s,"italic"))run.Append(new W.Italic());
                run.Append(new W.Color { Val=Str(s,"color","202020") },new W.FontSize { Val=FontHalfPoints(s).ToString(System.Globalization.CultureInfo.InvariantCulture) });
                stylePart.Styles.Append(new W.Style(new W.StyleName { Val=Str(s,"id") },paragraph,run)
                    { Type=W.StyleValues.Paragraph,StyleId=Str(s,"id"),Default=Str(s,"id")=="Normal" });
            }
            var numbering=main.AddNewPart<NumberingDefinitionsPart>();numbering.Numbering=new W.Numbering();
            foreach(var number in new[]{1,2})
            {
                var level=new W.Level(new W.StartNumberingValue { Val=1 },
                    new W.NumberingFormat { Val=number==1 ? W.NumberFormatValues.Bullet : W.NumberFormatValues.Decimal },
                    new W.LevelText { Val=number==1 ? "•" : "%1." },new W.LevelJustification { Val=W.LevelJustificationValues.Left },
                    new W.PreviousParagraphProperties(new W.Indentation { Left="360",Hanging="240" })) { LevelIndex=0 };
                numbering.Numbering.Append(new W.AbstractNum(level) { AbstractNumberId=number });
            }
            numbering.Numbering.Append(new W.NumberingInstance(new W.AbstractNumId { Val=1 }) { NumberID=1 },
                                       new W.NumberingInstance(new W.AbstractNumId { Val=2 }) { NumberID=2 });
            var page=args.GetProperty("page");int width=Int(page,"width_twips",11906),height=Int(page,"height_twips",16838),margin=Int(page,"margin_twips",1134);
            var body=main.Document.Body!;
            var blocks=args.GetProperty("blocks").EnumerateArray().ToList();bool pendingBreak=false;
            for(int blockIndex=0;blockIndex<blocks.Count;blockIndex++)
            {
                var b=blocks[blockIndex];
                if(Str(b,"kind")=="page_break")
                {
                    if(blockIndex==0 || blockIndex==blocks.Count-1 || pendingBreak)
                        throw new InvalidDataException("Page breaks must be between content blocks without adjacent breaks");
                    pendingBreak=true;continue;
                }
                if(Str(b,"kind")=="table_of_contents")
                {
                    body.Append(Contents.Create(Int(b,"max_level",3),pendingBreak));
                }
                else if(Str(b,"kind")=="paragraph")
                {
                    var p=Paragraph(Str(b,"text"),Str(b,"style","Normal"));var properties=p.ParagraphProperties!;
                    if(pendingBreak || Bool(b,"page_break_before"))properties.AddChild(new W.PageBreakBefore());
                    var kind=Str(b,"list_kind","none");
                    if(kind!="none")properties.AddChild(new W.NumberingProperties(new W.NumberingLevelReference { Val=0 },new W.NumberingId { Val=kind=="bullet"?1:2 }));
                    body.Append(p);
                }
                else
                {
                    if(Str(b,"kind")!="table")throw new InvalidDataException("Unknown creation block");
                    if(pendingBreak)
                    {
                        // A table needs a paragraph anchor for a reliable page break.
                        // Keep the empty anchor small and attached to its following table.
                        body.Append(new W.Paragraph(new W.ParagraphProperties(new W.KeepNext(),new W.PageBreakBefore(),
                            new W.SpacingBetweenLines { Before="0",After="0",Line="20",LineRule=W.LineSpacingRuleValues.Exact },
                            new W.ParagraphMarkRunProperties(new W.FontSize { Val="2" }))));
                    }
                    var rows=b.GetProperty("rows").EnumerateArray().ToList();int columns=rows[0].GetArrayLength();
                    int cellWidth=(width-2*margin)/columns;
                    var table=new W.Table(new W.TableProperties(new W.TableWidth { Type=W.TableWidthUnitValues.Dxa,Width=(width-2*margin).ToString() },
                        new W.TableBorders(new W.TopBorder { Val=W.BorderValues.Single,Size=4 },new W.LeftBorder { Val=W.BorderValues.Single,Size=4 },
                            new W.BottomBorder { Val=W.BorderValues.Single,Size=4 },new W.RightBorder { Val=W.BorderValues.Single,Size=4 },
                            new W.InsideHorizontalBorder { Val=W.BorderValues.Single,Size=4 },new W.InsideVerticalBorder { Val=W.BorderValues.Single,Size=4 })));
                    var grid=new W.TableGrid();for(int c=0;c<columns;c++)grid.Append(new W.GridColumn { Width=cellWidth.ToString() });table.Append(grid);
                    for(int r=0;r<rows.Count;r++)
                    {
                        var row=new W.TableRow();if(r==0 && Bool(b,"header_row",true))row.Append(new W.TableRowProperties(new W.TableHeader()));
                        foreach(var cell in rows[r].EnumerateArray())
                            row.Append(new W.TableCell(new W.TableCellProperties(new W.TableCellWidth { Type=W.TableWidthUnitValues.Dxa,Width=cellWidth.ToString() }),Paragraph(cell.GetString()!)));
                        table.Append(row);
                    }
                    body.Append(table);
                }
                pendingBreak=false;
            }
            var section=new W.SectionProperties();
            bool hasBands=false,hasFirst=false,hasEven=false;
            // Keep all header references before footer references in sectPr schema order.
            foreach(var story in new[]{"header","footer"})
            foreach(var variant in new[]{"default","first","even"})
            {
                var key=variant=="default"?story:variant+"_"+story;
                if(!args.TryGetProperty(key,out var band) || band.ValueKind!=JsonValueKind.Object)continue;
                var type=variant switch { "first"=>W.HeaderFooterValues.First,"even"=>W.HeaderFooterValues.Even,_=>W.HeaderFooterValues.Default };
                if(story=="header")
                {
                    var part=main.AddNewPart<HeaderPart>();part.Header=new W.Header(Band(band));part.Header.Save();
                    section.Append(new W.HeaderReference { Type=type,Id=main.GetIdOfPart(part) });
                }
                else
                {
                    var part=main.AddNewPart<FooterPart>();part.Footer=new W.Footer(Band(band));part.Footer.Save();
                    section.Append(new W.FooterReference { Type=type,Id=main.GetIdOfPart(part) });
                }
                hasBands=true;hasFirst|=variant=="first";hasEven|=variant=="even";
            }
            if(hasBands || blocks.Any(b=>Str(b,"kind")=="table_of_contents"))
            {
                var settings=main.AddNewPart<DocumentSettingsPart>();settings.Settings=new W.Settings();
                if(hasEven)settings.Settings.AddChild(new W.EvenAndOddHeaders { Val=true });
                settings.Settings.AddChild(new W.UpdateFieldsOnOpen { Val=true });settings.Settings.Save();
            }
            section.Append(new W.PageSize { Width=(uint)width,Height=(uint)height,Orient=width>height?W.PageOrientationValues.Landscape:W.PageOrientationValues.Portrait },
                new W.PageMargin { Top=margin,Bottom=margin,Left=(uint)margin,Right=(uint)margin,Header=(uint)Int(page,"header_distance_twips",567),Footer=(uint)Int(page,"footer_distance_twips",567),Gutter=0 });
            if(hasFirst)section.AddChild(new W.TitlePage { Val=true });
            body.Append(section);
            var error=new OpenXmlValidator().Validate(doc).FirstOrDefault();
            if(error!=null)throw new InvalidDataException("Creation validation failed: "+error.Description);
            main.Document.Save();stylePart.Styles.Save();numbering.Numbering.Save();
        }
        return memory.ToArray();
    }
}
