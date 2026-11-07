"""Phrase banks for synthetic conversations in a generic customer-support setting.

Names are invented. Phone numbers use the 555-01XX range reserved for fiction and e-mail/web
addresses use example.com/example.org (RFC 2606), so generated data never points at real people.
Short acknowledgements are included on purpose: they are meaningful replies, not noise.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PhraseBank:
    owner_name: str
    contact_names: tuple[str, ...]
    owner_openers: tuple[str, ...]
    owner_replies: tuple[str, ...]
    contact_openers: tuple[str, ...]
    contact_replies: tuple[str, ...]
    acknowledgements: tuple[str, ...]
    captions: tuple[str, ...]
    auto_reply: str
    multiline: str
    with_link: str
    with_contact_details: str


PHRASES = {
    "en": PhraseBank(
        owner_name="Northwind Support",
        contact_names=(
            "Alex Rivera",
            "Jamie Chen",
            "Morgan Patel",
            "Taylor Brooks",
            "Riley Novak",
            "Casey Duarte",
            "Jordan Okafor",
            "Avery Lindqvist",
            "Quinn Moreau",
            "Sam Whitaker",
            "Drew Kowalski",
            "Robin Achebe",
        ),
        owner_openers=(
            "Hi! Just checking in about your recent order.",
            "Hello, your replacement part is ready for pickup.",
            "Good morning! Do you still need help with the installation?",
            "Hi, we have an update on your support ticket.",
        ),
        owner_replies=(
            "Thanks for letting us know. I'll look into it right away.",
            "Could you send me the order number, please?",
            "The technician can visit on Thursday afternoon. Does that work?",
            "I've just sent the invoice to your e-mail.",
            "Sorry about the delay. The package left our warehouse today.",
            "Is there anything else I can help you with?",
        ),
        contact_openers=(
            "Hello, my order still hasn't arrived.",
            "Hi! Can I change the delivery address?",
            "The device stopped charging this morning.",
            "Do you have the blue model in stock?",
            "Good afternoon, I need a copy of my receipt.",
        ),
        contact_replies=(
            "The order number is 48213.",
            "Thursday works for me, thanks.",
            "I received it, everything looks fine now.",
            "It still shows as pending on the website.",
            "Can you call me after 5 pm?",
            "Great, thank you for the quick answer!",
        ),
        acknowledgements=("ok", "yes", "thanks!", "sure", "no", "👍", "done"),
        captions=("Here is the photo of the label", "This is the error on the screen"),
        auto_reply=(
            "Thank you for your message. We are away right now and will reply as soon as possible."
        ),
        multiline=(
            "Here are the details:\n- order 48213\n- two items\n"
            "12/03 works for the delivery, after 6 pm"
        ),
        with_link="You can track it here: https://tracking.example.com/p/48213",
        with_contact_details="Sure, call me at +1 555-0142 or write to alex@example.com",
    ),
    "pt-BR": PhraseBank(
        owner_name="Suporte Exemplo",
        contact_names=(
            "Ana Ribeiro",
            "Bruno Tavares",
            "Carla Menezes",
            "Diego Fontana",
            "Elisa Prado",
            "Felipe Arruda",
            "Gabriela Nunes",
            "Heitor Campos",
            "Isabela Rocha",
            "João Vasconcelos",
            "Larissa Teles",
            "Marcos Peixoto",
        ),
        owner_openers=(
            "Olá! Passando para saber do seu pedido recente.",
            "Oi, sua peça de reposição já está disponível para retirada.",
            "Bom dia! Ainda precisa de ajuda com a instalação?",
            "Olá, temos uma atualização sobre o seu chamado.",
        ),
        owner_replies=(
            "Obrigado por avisar. Vou verificar agora mesmo.",
            "Pode me enviar o número do pedido, por favor?",
            "O técnico pode ir na quinta à tarde. Fica bom para você?",
            "Acabei de enviar a nota fiscal para o seu e-mail.",
            "Desculpe a demora. O pacote saiu do nosso estoque hoje.",
            "Posso ajudar com mais alguma coisa?",
        ),
        contact_openers=(
            "Olá, meu pedido ainda não chegou.",
            "Oi! Consigo mudar o endereço de entrega?",
            "O aparelho parou de carregar hoje de manhã.",
            "Vocês têm o modelo azul em estoque?",
            "Boa tarde, preciso de uma segunda via do recibo.",
        ),
        contact_replies=(
            "O número do pedido é 48213.",
            "Quinta fica ótimo, obrigada.",
            "Recebi, agora está tudo certo.",
            "No site ainda aparece como pendente.",
            "Pode me ligar depois das 17h?",
            "Perfeito, obrigado pela resposta rápida!",
        ),
        acknowledgements=("ok", "sim", "obrigado!", "certo", "não", "👍", "feito"),
        captions=("Segue a foto da etiqueta", "Esse é o erro que aparece na tela"),
        auto_reply=(
            "Agradecemos o seu contato. No momento não estamos disponíveis e responderemos "
            "assim que possível."
        ),
        multiline=(
            "Seguem os dados:\n- pedido 48213\n- dois itens\n"
            "12/03 serve para a entrega, depois das 18h"
        ),
        with_link="Você pode acompanhar aqui: https://rastreio.example.com/p/48213",
        with_contact_details="Claro, me liga no +1 555-0142 ou escreve para ana@example.org",
    ),
}
