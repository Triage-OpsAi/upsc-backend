"""Deterministically seed a complete Polity Chapter 1 question set."""

import asyncio

from app.database import close_pool, get_pool


ITEMS = [
    ("Constituent Assembly composition", [
        "The Constituent Assembly was originally to have 389 members.",
        "Seats allotted to British Indian provinces were filled by direct adult-franchise elections.",
        "Princely States were allotted 93 seats in the original scheme.",
    ], 1, "Provincial representatives were indirectly elected by members of the Provincial Legislative Assemblies, not directly by the adult electorate."),
    ("Constituent Assembly election", [
        "Provincial Assembly members elected Constituent Assembly representatives by proportional representation through the single transferable vote.",
        "The princely states were to choose their representatives through nomination.",
        "Separate electorates in the Assembly were abolished by the Cabinet Mission Plan itself before the 1946 elections.",
    ], 2, "The Cabinet Mission scheme distributed provincial seats among communities; the election was not conducted on the basis that all separate communal representation had already been abolished."),
    ("Opening of the Constituent Assembly", [
        "The Constituent Assembly first met on 9 December 1946.",
        "Dr Sachchidananda Sinha served as the temporary President at its opening.",
        "Dr Rajendra Prasad was elected permanent President on 9 December 1946 itself.",
    ], 2, "Dr Rajendra Prasad was elected permanent President on 11 December 1946, not on the opening day."),
    ("Major Constituent Assembly committees", [
        "Jawaharlal Nehru chaired the Union Powers Committee.",
        "Vallabhbhai Patel chaired the Advisory Committee on Fundamental Rights, Minorities and Tribal and Excluded Areas.",
        "B. R. Ambedkar chaired the Provincial Constitution Committee.",
    ], 2, "Vallabhbhai Patel, not B. R. Ambedkar, chaired the Provincial Constitution Committee."),
    ("Drafting machinery", [
        "The Drafting Committee was appointed on 29 August 1947.",
        "B. N. Rau served as Constitutional Adviser to the Constituent Assembly.",
        "The Drafting Committee as originally constituted had nine members.",
    ], 2, "The Drafting Committee as originally constituted had seven members, not nine."),
    ("Objectives Resolution", [
        "Jawaharlal Nehru moved the Objectives Resolution on 13 December 1946.",
        "The Constituent Assembly adopted it on 22 January 1947.",
        "The Resolution became legally enforceable as a separate Schedule to the Constitution.",
    ], 2, "The Objectives Resolution supplied the philosophy later reflected in the Preamble; it did not become an enforceable Schedule."),
    ("Adoption and commencement", [
        "The Constituent Assembly adopted the Constitution on 26 November 1949.",
        "Members signed the Constitution on 24 January 1950.",
        "Every provision of the Constitution commenced only on 26 January 1950.",
    ], 2, "Articles including 5 to 9, 60, 324, 366, 367, 379 and 380 came into force on 26 November 1949; the remaining provisions commenced on 26 January 1950."),
    ("Time taken by the Constituent Assembly", [
        "The Constituent Assembly took two years, eleven months and eighteen days to complete the Constitution.",
        "It held eleven sessions during this work.",
        "The Assembly spent fewer than fifty days considering the Draft Constitution.",
    ], 2, "The Assembly spent 114 days considering the Draft Constitution, not fewer than fifty."),
    ("Government of India Act 1935 influence", [
        "A substantial part of the Constitution's administrative detail drew on the Government of India Act, 1935.",
        "The office of Governor and the broad federal scheme show that influence.",
        "The Constitution copied the 1935 Act's system of dyarchy in the provinces unchanged.",
    ], 2, "The Constitution did not continue provincial dyarchy; the 1935 Act had itself abolished provincial dyarchy and introduced provincial autonomy."),
    ("British and United States influences", [
        "The parliamentary system and cabinet responsibility reflect British constitutional practice.",
        "Fundamental Rights and judicial review show United States influence.",
        "The procedure established by law in Article 21 was borrowed from the United States Constitution.",
    ], 2, "The expression 'procedure established by law' was drawn from the Japanese Constitution, not the United States Constitution."),
    ("Irish and Canadian influences", [
        "The Directive Principles of State Policy were influenced by the Irish Constitution.",
        "The nomination of members to the Rajya Sabha also reflects an Irish feature.",
        "Residuary powers vested in the Union were borrowed from the Australian Constitution.",
    ], 2, "The allocation of residuary powers to the Union reflects the Canadian model, not the Australian model."),
    ("Australian and South African influences", [
        "The Concurrent List and freedom of inter-State trade show Australian influence.",
        "The procedure for constitutional amendment and election of Rajya Sabha members show South African influence.",
        "The idea of a joint sitting of the two Houses was borrowed from Canada.",
    ], 2, "The joint-sitting device was borrowed from Australia, not Canada."),
    ("Text of the Preamble", [
        "The Forty-second Amendment inserted the words Socialist and Secular into the Preamble.",
        "The same amendment changed 'unity of the Nation' to 'unity and integrity of the Nation'.",
        "The word Republic was inserted into the Preamble by the Forty-second Amendment.",
    ], 2, "Republic formed part of the Preamble as originally adopted; it was not inserted by the Forty-second Amendment."),
    ("Preamble and the basic structure", [
        "In the Berubari Union opinion, the Supreme Court said the Preamble was not a part of the Constitution.",
        "In Kesavananda Bharati, the Court held that the Preamble is part of the Constitution.",
        "Kesavananda Bharati held that Parliament can amend the Preamble even so as to destroy the Constitution's basic structure.",
    ], 2, "Kesavananda Bharati accepted amendment of the Preamble under Article 368 but not destruction of the basic structure."),
    ("Legal role of the Preamble", [
        "The Preamble can assist in interpreting ambiguous constitutional language.",
        "The Preamble is not by itself a source of legislative power.",
        "A citizen can directly enforce every Preamble ideal through a writ even without a supporting constitutional provision.",
    ], 2, "The Preamble is non-justiciable by itself; enforceable rights must rest on operative constitutional provisions."),
    ("Federal and unitary features", [
        "A written supreme Constitution and division of powers are federal features.",
        "Single citizenship and an integrated judiciary add unitary features.",
        "The Constitution describes India as a Federation of States in Article 1.",
    ], 2, "Article 1 describes India, that is Bharat, as a Union of States, not a Federation of States."),
    ("Rigidity and flexibility", [
        "Some constitutional provisions can be changed by a simple parliamentary majority outside Article 368.",
        "Specified federal provisions require ratification by at least half of the State Legislatures after Parliament passes the amendment.",
        "Every constitutional change requires a referendum of the electorate.",
    ], 2, "The Constitution prescribes no referendum requirement for constitutional amendments."),
    ("Parliamentary government", [
        "The Council of Ministers is collectively responsible to the Lok Sabha.",
        "The President is the constitutional head while the Council of Ministers exercises real executive authority.",
        "The Indian parliamentary system rests on a strict separation of executive and legislature.",
    ], 2, "Parliamentary government entails fusion and coordination between executive and legislature, not strict separation."),
    ("Universal adult suffrage", [
        "Article 326 provides for elections to the House of the People and State Legislative Assemblies on the basis of adult suffrage.",
        "The Constitution originally fixed the voting age at twenty-one years.",
        "The Forty-fourth Amendment lowered the voting age from twenty-one to eighteen years.",
    ], 2, "The Sixty-first Amendment, not the Forty-fourth, lowered the voting age to eighteen years."),
    ("Citizenship under Article 5", [
        "Article 5 operated at the commencement of the Constitution.",
        "Domicile in India was an essential condition under Article 5.",
        "Birth in India alone made every person a citizen under Article 5 regardless of domicile.",
    ], 2, "Article 5 required domicile plus one of the listed connections: birth, a parent's birth, or five years' ordinary residence immediately before commencement."),
    ("Migrants from Pakistan under Article 6", [
        "Article 6 addressed certain persons who migrated to India from territory then included in Pakistan.",
        "For migration on or after 19 July 1948, registration was required subject to the constitutional conditions.",
        "A post-19 July 1948 applicant needed no period of residence in India before applying for registration.",
    ], 2, "A post-19 July 1948 applicant had to be resident in India for at least six months immediately before the application."),
    ("Migrants to Pakistan under Article 7", [
        "Article 7 concerns persons who migrated from India to Pakistan after 1 March 1947.",
        "Its exception covers return under a permit for resettlement or permanent return.",
        "Every person who once migrated to Pakistan was absolutely barred from citizenship even after returning under such a permit.",
    ], 2, "Article 7 contains an exception for a person returning under a permit for resettlement or permanent return, who is then treated under Article 6's post-19 July 1948 route."),
    ("Citizens abroad under Article 8", [
        "Article 8 covers certain persons of Indian origin ordinarily residing outside India.",
        "Registration may be made by an Indian diplomatic or consular representative in the country of residence.",
        "Article 8 citizenship arose automatically without an application for registration.",
    ], 2, "Article 8 requires registration on an application made to the appropriate Indian diplomatic or consular representative."),
    ("Articles 9 to 11", [
        "Article 9 excludes a person who voluntarily acquired citizenship of a foreign State from citizenship under Articles 5, 6 or 8.",
        "Article 10 continues citizenship subject to laws made by Parliament.",
        "Article 11 permanently prevents Parliament from regulating acquisition and termination of citizenship.",
    ], 2, "Article 11 expressly empowers Parliament to regulate citizenship acquisition, termination and all other citizenship matters."),
    ("Article 1 territory", [
        "Article 1 names the country 'India, that is Bharat'.",
        "The territory of India includes States, Union territories specified in the First Schedule, and territories that may be acquired.",
        "Article 1 declares each State to possess an unconditional constitutional right to secede from the Union.",
    ], 2, "The Constitution describes a Union of States and confers no constitutional right of secession."),
    ("Articles 2 and 3", [
        "Article 2 empowers Parliament to admit into the Union or establish new States on terms it thinks fit.",
        "Article 3 covers formation of new States and alteration of areas, boundaries or names of existing States.",
        "A law under Article 3 can be introduced in Parliament without the President's recommendation.",
    ], 2, "An Article 3 bill may be introduced only on the President's recommendation."),
    ("State views and Schedule changes", [
        "When an Article 3 proposal affects a State's area, boundary or name, the President refers it to that State Legislature for its views.",
        "Parliament is not constitutionally bound by the State Legislature's views.",
        "A law under Articles 2 or 3 that changes the First or Fourth Schedule is deemed an Article 368 amendment.",
    ], 2, "Article 4 says such a law is not deemed an amendment of the Constitution for Article 368 purposes and it may be passed by the ordinary legislative process."),
]


COMBINATION_OPTIONS = [
    {"key": "A", "text": "1 and 2 only"},
    {"key": "B", "text": "2 and 3 only"},
    {"key": "C", "text": "1 and 3 only"},
    {"key": "D", "text": "1, 2 and 3"},
]


def _correct_option(false_index: int) -> str:
    return {0: "B", 1: "C", 2: "A"}[false_index]


def _question_text(concept: str, statements: list[str]) -> str:
    lines = "\n".join(f"{index}. {statement}" for index, statement in enumerate(statements, 1))
    return f"With reference to {concept}, consider the following statements:\n\n{lines}\n\nWhich of the statements given above are correct?"


async def main(target: int = 30) -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            chapter_id = await conn.fetchval(
                """
                insert into subject_chapters (subject_key, name, chapter_order)
                values ('polity','Constitutional Framework',1)
                on conflict (subject_key, name) do update set name=excluded.name
                returning id
                """
            )
            existing = await conn.fetchval(
                "select count(*) from subject_questions where chapter_id=$1", chapter_id
            )
            for bank_index, (concept, statements, false_index, correction) in enumerate(ITEMS):
                if existing >= target:
                    break
                rotation = bank_index % len(statements)
                statements = statements[rotation:] + statements[:rotation]
                false_index = (false_index - rotation) % len(statements)
                text = _question_text(concept, statements)
                if await conn.fetchval(
                    "select exists(select 1 from subject_questions where chapter_id=$1 and question_text=$2)",
                    chapter_id,
                    text,
                ):
                    continue
                correct = _correct_option(false_index)
                true_numbers = [str(index + 1) for index in range(3) if index != false_index]
                explanation = (
                    f"Statements {' and '.join(true_numbers)} are correct. Statement {false_index + 1} is incorrect. "
                    f"{correction} Correct option is {correct}: "
                    f"{next(option['text'] for option in COMBINATION_OPTIONS if option['key'] == correct)}"
                )
                practice_options = [
                    {"key": chr(65 + index), "text": statement}
                    for index, statement in enumerate(statements)
                ] + [{"key": "D", "text": "All three statements are accurate."}]
                slides = [
                    (1, "theory", f"{concept}: precision distinction", f"Precision hinge: {correction}", None, None, None, None),
                    (2, "theory", f"{concept}: complete rule", explanation, None, None, None, None),
                    (3, "practice", f"{concept}: identify the error", None, f"Which one of the following statements about {concept} is inaccurate?", practice_options, chr(65 + false_index), correction),
                    (4, "practice", f"{concept}: combination check", None, _question_text(concept, statements), COMBINATION_OPTIONS, correct, explanation),
                ]
                async with conn.transaction():
                    question_id = await conn.fetchval(
                        """
                        insert into subject_questions
                          (chapter_id, question_text, options, correct_option, explanation, difficulty, format)
                        values ($1,$2,$3,$4,$5,'very_hard','statement') returning id
                        """,
                        chapter_id, text, COMBINATION_OPTIONS, correct, explanation,
                    )
                    for slide in slides:
                        await conn.execute(
                            """
                            insert into subject_breakdown_slides
                              (question_id, slide_order, slide_type, concept, content,
                               practice_question, practice_options, practice_correct_option,
                               practice_explanation)
                            values ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                            """,
                            question_id, *slide,
                        )
                existing += 1
                print(f"Stored {existing}/{target}: {concept}", flush=True)
            if existing < target:
                raise RuntimeError(f"Curated bank only reached {existing}/{target} questions")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
